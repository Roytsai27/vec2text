import logging
import os
import random
from typing import Any, Dict, List, Optional, Tuple, Union

import datasets
import torch
import torch.nn as nn

from models import CorrectorEncoderModel, CorrectorModel
from models.model_utils import freeze_params
from run_args import TrainingArguments

from .base import BaseTrainer
from .inversion import InversionTrainer

logger = logging.getLogger(__name__)


class CorrectorTrainer(BaseTrainer):
    """Trains an encoder model to generate embeddings that recursively correct of an
    InversionTrainer.
    """

    # TODO: don't assume that the encoder has to have the same tokenizer as the encoder_decoder
    # or embedder model.

    _hypothesis_cache: Dict[str, Tuple[torch.Tensor, torch.Tensor, torch.Tensor]]

    # If set, only take hypothesis if it improves our distance to ground-truth.
    return_best_hypothesis: bool = False

    # Initialize from this hypothesis, if set
    initial_hypothesis_str: Optional[str] = None

    def __init__(
        self,
        model: Union[CorrectorEncoderModel, CorrectorModel],
        inversion_trainer: InversionTrainer,
        args: TrainingArguments,
    ):
        # Freeze other model params
        freeze_params(inversion_trainer.model)
        # We're training this corrector model to correct outputs from
        # a model trained & loaded via the inversion trainer.
        self.inversion_trainer = inversion_trainer
        self.inversion_trainer.model.use_frozen_embeddings_as_input = True
        super().__init__(
            model=model,
            args=args,
            train_dataset=self.inversion_trainer.train_dataset,
            eval_dataset=self.inversion_trainer.eval_dataset,
            data_collator=self.inversion_trainer.data_collator,
        )
        self.tokenizer = self.inversion_trainer.model.tokenizer
        self.embedder_tokenizer = self.inversion_trainer.model.embedder_tokenizer
        self.call_embedding_model = self.inversion_trainer.model.call_embedding_model

        self.initial_hypothesis_str = None

        # Number of steps of self-correction
        self.num_gen_recursive_steps = 1

        # If set, return closest (in embedding space) hypothesis we see during generation
        self.return_best_hypothesis = False

        # Initialize our model with pre-trained model params
        missing_keys, unexpected_keys = self.model.load_state_dict(
            self.inversion_trainer.model.state_dict(), strict=False
        )

        # Need to train with same device as the inversion model to avoid weird errors.
        assert self.args.fp16 == self.inversion_trainer.args.fp16
        assert self.args.bf16 == self.inversion_trainer.args.bf16

    def _precompute_hypothesis_and_embedding(
        self, ds_inputs: Dict[str, torch.Tensor]
    ) -> Dict[str, torch.Tensor]:
        inputs = {k: torch.tensor(v) for k, v in ds_inputs.items()}
        inputs = {k: v.to(self.args.device) for k, v in inputs.items()}
        (
            frozen_embeddings,
            hypothesis_input_ids,
            hypothesis_attention_mask,
            hypothesis_embedding,
        ) = self._get_hypothesis_uncached(inputs=inputs)
        ds_inputs["frozen_embeddings"] = frozen_embeddings.cpu()
        ds_inputs["hypothesis_input_ids"] = hypothesis_input_ids.cpu()
        ds_inputs["hypothesis_attention_mask"] = hypothesis_attention_mask.cpu()
        ds_inputs["hypothesis_embedding"] = hypothesis_embedding.cpu()
        return ds_inputs

    def _preprocess_dataset(self, dataset: datasets.Dataset) -> datasets.Dataset:
        #
        # In each model directory, we store a copy of the dataset with hypotheses
        # generated by the model that's checkpointed in this directory. This
        # won't scale well, but hopefully we don't do this with too many models,
        # and precomputing 5M hypotheses on A100 takes ~8 hours, so they're worth
        # storing.
        #
        # Note that the dataset fingerprint changes with calls to select()
        # so we won't overwrite the big dataset files when we use tiny subsets
        # during testing.
        root_dir = os.path.normpath(
            os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir)
        )
        model_dir = os.path.join(root_dir, self.inversion_trainer.args.output_dir)
        assert os.path.exists(model_dir)
        cache_path = os.path.join(model_dir, f"{dataset._fingerprint}_hypotheses.cache")
        if not os.path.exists(cache_path):
            logging.info("Computing hypotheses to save to path %s", cache_path)
            print(f"Saving hypotheses to path {cache_path}")
            dataset = dataset.map(
                self._precompute_hypothesis_and_embedding,
                batched=True,
                batch_size=self.args.train_batch_size * 4,
                desc="Precomputing hypotheses for data",
            )
            dataset.save_to_disk(cache_path)
        else:
            logging.info("Loading hypotheses from path %s", cache_path)
            print(f"Loading hypotheses from path {cache_path}")
            dataset = datasets.load_from_disk(cache_path)
        return dataset
    
    def precompute_hypotheses(self) -> None:
        logger.info("Precomputing frozen embedding & hypotheses before training")
        self.train_dataset = self._preprocess_dataset(dataset=self.train_dataset)
        for k, v in self.eval_dataset.items():
            self.eval_dataset[k] = self._preprocess_dataset(dataset=v)

    def _inner_training_loop(self, *args, **kwargs):
        self.precompute_hypotheses()

        return super()._inner_training_loop(*args, **kwargs)

    def generate(self, inputs: Dict, generation_kwargs: Dict, num_recursive_steps: int = None,
        num_recursive_steps_so_far: int = 0) -> torch.Tensor:
        """Generates text using self-correction.

        Args:
            inputs (Dict[str, torch.Tensor]): inputs for generation, like the input embedding, hypothesis,
                and hypothesis embedding
            generation_kwargs (Dict): dictionary of parameters for generation, will be passed on to the model
            num_recursive_steps (int): Number of remaining steps of recursion, used to know when to stop
            num_recusive_steps_so_far (int): Number of steps of recursion performed so far. This is how we
                can check if it's the initial hypothesis or not.
        Returns:
            generated_ids (torch.Tensor): ids of generated text
        """
        if num_recursive_steps is None:
            num_recursive_steps = self.num_gen_recursive_steps

        try:
            frozen_embeddings = inputs["frozen_embeddings"]
            hypothesis_input_ids = inputs["hypothesis_input_ids"]
            hypothesis_attention_mask = inputs["hypothesis_attention_mask"]
            hypothesis_embedding = inputs["hypothesis_embedding"]
        except KeyError:
            (
                frozen_embeddings,
                hypothesis_input_ids,
                hypothesis_attention_mask,
                hypothesis_embedding,
            ) = self._get_hypothesis_uncached(inputs=inputs)
        inputs["frozen_embeddings"] = frozen_embeddings
        inputs["hypothesis_input_ids"] = hypothesis_input_ids
        inputs["hypothesis_attention_mask"] = hypothesis_attention_mask
        inputs["hypothesis_embedding"] = hypothesis_embedding

        if (num_recursive_steps_so_far == 0) and (self.initial_hypothesis_str is not None):
            print("using initial hypothesis")
            # If set, uses this string as the hypothesis for step 0 of self-correction
            batch_size = frozen_embeddings.shape[0]
            gen_text_ids = self.embedder_tokenizer(
                [self.initial_hypothesis_str],
                return_tensors="pt",
                max_length=hypothesis_input_ids.shape[1],
                truncation=True,
                padding="max_length",
            )["input_ids"].repeat((batch_size, 1)).to(self.args.device)
            # gen_text_ids = (
            #     torch.randint(low=1, high=self.embedder_tokenizer.vocab_size, size=(1, 32), dtype=torch.long)
            #     .repeat((batch_size, 1)).to(self.args.device)
            # )
            bos_token_id = self.model.encoder_decoder.config.decoder_start_token_id
            bos_token_ids = torch.ones((batch_size, 1), dtype=torch.long, device=gen_text_ids.device) * bos_token_id
            gen_text_ids = torch.cat(
                (bos_token_ids, gen_text_ids[:, :-1]), dim=1
            )
        
        elif self.is_corrector_encoder:
            gen_text_ids = self.model.generate(
                inputs=inputs,
                generation_kwargs=generation_kwargs,
            )
        else:
            gen_text_ids = self.model.generate(
                inputs=inputs,
                generation_kwargs=generation_kwargs,
                embed_generated_hypothesis_func=self.embed_generated_hypothesis,
            )
            # Don't return <hypothesis><text> upon generation, just return <text>
            max_length = inputs.get("input_ids", inputs["embedder_input_ids"]).shape[1]
            gen_text_ids = gen_text_ids[:, max_length:]
        
        
        hypothesis_embedding = self.embed_generated_hypothesis(input_ids=gen_text_ids)
        # Make sure we generated the proper number of tokens
        # assert gen_text_ids.shape[1] in [32, 128]

        # Track best one we've seen so far.
        best_hypothesis_input_ids = inputs.get("best_hypothesis_input_ids", inputs["hypothesis_input_ids"])
        best_hypothesis_embedding = inputs.get("best_hypothesis_embedding", inputs["hypothesis_embedding"])
        best_distance = 1.0 - torch.nn.CosineSimilarity(dim=1)(inputs["frozen_embeddings"], best_hypothesis_embedding)
        new_distance = 1.0 - torch.nn.CosineSimilarity(dim=1)(inputs["frozen_embeddings"], hypothesis_embedding)
        inputs["best_hypothesis_input_ids"] = torch.where(
            (best_distance < new_distance)[:, None], best_hypothesis_input_ids, gen_text_ids
        )
        # if 0 < (best_distance < new_distance).float().mean() < 1: import pdb; pdb.set_trace()
        inputs["best_hypothesis_embedding"] = torch.where(
            (best_distance < new_distance)[:, None], best_hypothesis_embedding, hypothesis_embedding
        )
        
        if num_recursive_steps == 1:
            if self.return_best_hypothesis:
                return inputs["best_hypothesis_input_ids"]
            else:
                return gen_text_ids
        else:
            inputs["hypothesis_input_ids"] = gen_text_ids
            inputs["hypothesis_attention_mask"] = (gen_text_ids != self.model.encoder_decoder.config.pad_token_id).int()
            inputs["hypothesis_embedding"] = hypothesis_embedding
            return self.generate(
                inputs=inputs, generation_kwargs=generation_kwargs, 
                num_recursive_steps=(num_recursive_steps-1),
                num_recursive_steps_so_far=num_recursive_steps_so_far+1,
            )

    @property
    def is_corrector_encoder(self):
        return isinstance(self.model, CorrectorEncoderModel)

    def get_frozen_embeddings(
        self,
        embedder_input_ids: torch.Tensor,
        embedder_attention_mask: torch.Tensor,
    ) -> torch.Tensor:
        with torch.no_grad():
            frozen_embeddings = self.inversion_trainer.call_embedding_model(
                input_ids=embedder_input_ids,
                attention_mask=embedder_attention_mask,
            )
        return frozen_embeddings

    def embed_generated_hypothesis(self, input_ids: torch.Tensor) -> torch.Tensor:
        """Embeds a generated hypothesis. Has to remove EOS token and add BOS token
        at the beginning.
        """
        bos_token_id = self.model.encoder_decoder.config.decoder_start_token_id
        eos_token_id = self.model.encoder_decoder.config.eos_token_id
        assert (input_ids[:, 0] == bos_token_id).all()
        batch_size = len(input_ids)
        eos_tokens = (
            torch.ones((batch_size, 1), dtype=torch.long, device=self.args.device)
            * eos_token_id
        )

        input_ids = torch.cat((input_ids[:, 1:], eos_tokens), dim=1)
        attention_mask = input_ids != self.model.encoder_decoder.config.pad_token_id
        return self.get_frozen_embeddings(
            embedder_input_ids=input_ids,
            embedder_attention_mask=attention_mask,
        )

    def _get_hypothesis_uncached(self, inputs: Dict[str, torch.Tensor]) -> torch.Tensor:
        batch_size, seq_length = inputs["embedder_input_ids"].shape
        fake_embedder_input_ids = torch.ones(
            (batch_size, seq_length), device=self.args.device
        )
        fake_embedder_attention_mask = torch.ones(
            (batch_size, seq_length), device=self.args.device
        )
        frozen_embeddings = self.get_frozen_embeddings(
            embedder_input_ids=inputs["embedder_input_ids"],
            embedder_attention_mask=inputs["embedder_attention_mask"],
        )

        # TODO: support generated outputs of varying length.
        # TODO consider other (multiple?) hypothesis generation conditions.
        hypothesis_input_ids = self.inversion_trainer.model.generate(
            inputs={
                "embedder_input_ids": fake_embedder_input_ids,
                "embedder_attention_mask": fake_embedder_attention_mask,
                "frozen_embeddings": frozen_embeddings,
            },
            generation_kwargs={
                "early_stopping": False,
                "num_beams": 1,
                "do_sample": False,
                "no_repeat_ngram_size": 0,
                "max_length": seq_length,
            },
        )
        hypothesis_attention_mask = (
            hypothesis_input_ids != self.model.encoder_decoder.config.pad_token_id
        )
        hypothesis_embedding = self.embed_generated_hypothesis(
            input_ids=hypothesis_input_ids
        )
        return (
            frozen_embeddings,
            hypothesis_input_ids,
            hypothesis_attention_mask,
            hypothesis_embedding,
        )

    def compute_loss(
        self,
        model: CorrectorModel,
        inputs: Dict[str, torch.Tensor],
        return_outputs: bool = False,
    ) -> Union[Tuple[torch.Tensor, Dict[str, torch.Tensor]], torch.Tensor]:
        """Computes contrastive loss using model generations and real text."""
        batch_size, seq_length = inputs["input_ids"].shape

        try:
            frozen_embeddings = inputs["frozen_embeddings"]
            hypothesis_input_ids = inputs["hypothesis_input_ids"]
            hypothesis_attention_mask = inputs["hypothesis_attention_mask"]
            hypothesis_embedding = inputs["hypothesis_embedding"]
        except KeyError:
            (
                frozen_embeddings,
                hypothesis_input_ids,
                hypothesis_attention_mask,
                hypothesis_embedding,
            ) = self._get_hypothesis_uncached(inputs=inputs)

        if self.is_corrector_encoder:
            labels = inputs["labels"]
            outputs = self.model(
                embedding=frozen_embeddings,
                hypothesis_embedding=hypothesis_embedding,
                hypothesis_input_ids=hypothesis_input_ids,
                hypothesis_attention_mask=hypothesis_attention_mask,
                labels=labels,
            )
        else:
            shift_right = self.model.encoder_decoder._shift_right
            # Special training scheme for the decoder-based model
            if self.model.training and random.random() < 0.5:
                # Half the time, we feed in a 'null' hypothesis embedding
                # and train the model to decode good hypotheses. The other
                # half of the time, we train it to correct its own hypotheses
                # using 'bad' hypotheses from the previous model.
                hypothesis_embedding = self.model.null_hypothesis_embedding(
                    hypothesis_embedding
                )
                # Will look like [...label_input_ids, 1] and get right-shifted to
                # [0, ..input_ids] by the model.
                decoder_input_ids = shift_right(inputs["labels"])
                labels = inputs["labels"]
            else:
                # Will look like [...hypothesis_input_ids, 1, ...label_ids, 1]
                # and get right-shifted to [0, ...hypothesis_input_ids, 1, ...label_ids]
                # by the model.
                # Do this always during evaluation, and 50% of the time during training.
                decoder_input_ids = shift_right(torch.cat((hypothesis_input_ids, inputs["labels"]), dim=1))
                empty_tokens = torch.ones_like(hypothesis_input_ids, device=hypothesis_input_ids.device) * -100
                labels = torch.cat((empty_tokens, inputs["labels"]), dim=1)
            outputs = self.model(
                embedding=frozen_embeddings,
                hypothesis_embedding=hypothesis_embedding,
                decoder_input_ids=decoder_input_ids,
                labels=labels,
            )
        return outputs.loss

    def prediction_step(
        self,
        model: nn.Module,
        inputs: Dict[str, Union[torch.Tensor, Any]],
        prediction_loss_only: bool,
        ignore_keys: Optional[List[str]] = None,
    ) -> Tuple[Optional[torch.Tensor], Optional[torch.Tensor], Optional[torch.Tensor]]:
        """
        Perform an evaluation step on `model` using `inputs`. Called during self.evalaute()
        """
        inputs = {key: value.to(self.args.device) for key, value in inputs.items()}
        with torch.no_grad():
            loss = self.compute_loss(model=model, inputs=inputs)
        
        logits, labels = None, None
        return loss, logits, labels
