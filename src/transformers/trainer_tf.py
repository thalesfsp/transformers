"""Tensorflow trainer class."""

import datetime
import logging
import math
import os
import sys
from typing import Callable, Dict, Optional, Tuple

import numpy as np
import tensorflow as tf
from packaging.version import parse

from .modeling_tf_utils import TFPreTrainedModel
from .optimization_tf import GradientAccumulator, create_optimizer
from .trainer_utils import PREFIX_CHECKPOINT_DIR, EvalPrediction, PredictionOutput, is_wandb_available, set_seed
from .training_args_tf import TFTrainingArguments


if is_wandb_available():
    import wandb


logger = logging.getLogger(__name__)


if parse(tf.__version__).release < (2, 2, 0):
    logger.info(
        "You need to run the TensorFlow trainer with at least the version 2.2.0, your version is {}".format(
            tf.__version__
        )
    )
    sys.exit(1)


class TFTrainer:
    """
    TFTrainer is a simple but feature-complete training and eval loop for TensorFlow,
    optimized for 🤗 Transformers.

    Args:
        model (:class:`~transformers.TFPreTrainedModel`):
            The model to train, evaluate or use for predictions.
        args (:class:`~transformers.TFTrainingArguments`):
            The arguments to tweak training.
        train_dataset (:class:`~tf.data.Dataset`, `optional`):
            The dataset to use for training.
        eval_dataset (:class:`~tf.data.Dataset`, `optional`):
            The dataset to use for evaluation.
        compute_metrics (:obj:`Callable[[EvalPrediction], Dict]`, `optional`):
            The function that will be used to compute metrics at evaluation. Must take a
            :class:`~transformers.EvalPrediction` and return a dictionary string to metric values.
        prediction_loss_only (:obj:`bool`, `optional`, defaults to `False`):
            When performing evaluation and predictions, only returns the loss.
        tb_writer (:obj:`tf.summary.SummaryWriter`, `optional`):
            Object to write to TensorBoard.
        optimizers (:obj:`Tuple[tf.keras.optimizers.Optimizer, tf.keras.optimizers.schedules.LearningRateSchedule]`, `optional`):
            A tuple containing the optimizer and the scheduler to use. The optimizer default to an instance of
            :class:`tf.keras.optimizers.Adam` if :obj:`args.weight_decay_rate` is 0 else an instance of
            :class:`~transformers.AdamWeightDecay`. The scheduler will default to an instance of
            :class:`tf.keras.optimizers.schedules.PolynomialDecay` if :obj:`args.num_warmup_steps` is 0 else
            an instance of :class:`~transformers.WarmUp`.
    """

    model: TFPreTrainedModel
    args: TFTrainingArguments
    train_dataset: Optional[tf.data.Dataset]
    eval_dataset: Optional[tf.data.Dataset]
    compute_metrics: Optional[Callable[[EvalPrediction], Dict]] = None
    prediction_loss_only: bool
    tb_writer: Optional[tf.summary.SummaryWriter] = None
    optimizers: Tuple[tf.keras.optimizers.Optimizer, tf.keras.optimizers.schedules.LearningRateSchedule] = (None, None)
    global_step: Optional[int] = None
    epoch_logging: Optional[float] = None

    def __init__(
        self,
        model: TFPreTrainedModel,
        args: TFTrainingArguments,
        train_dataset: Optional[tf.data.Dataset] = None,
        eval_dataset: Optional[tf.data.Dataset] = None,
        compute_metrics: Optional[Callable[[EvalPrediction], Dict]] = None,
        prediction_loss_only=False,
        tb_writer: Optional[tf.summary.SummaryWriter] = None,
        optimizers: Tuple[tf.keras.optimizers.Optimizer, tf.keras.optimizers.schedules.LearningRateSchedule] = (
            None,
            None,
        ),
    ):
        self.model = model
        self.args = args
        self.train_dataset = train_dataset
        self.eval_dataset = eval_dataset
        self.compute_metrics = compute_metrics
        self.prediction_loss_only = prediction_loss_only
        self.optimizer, self.lr_scheduler = optimizers
        self.gradient_accumulator = GradientAccumulator()
        self.global_step = 0
        self.epoch_logging = 0

        if tb_writer is not None:
            self.tb_writer = tb_writer
        else:
            self.tb_writer = tf.summary.create_file_writer(self.args.logging_dir)

        if is_wandb_available():
            self._setup_wandb()
        elif os.environ.get("WANDB_DISABLED") != "true":
            logger.info(
                "You are instantiating a Trainer but W&B is not installed. To use wandb logging, "
                "run `pip install wandb; wandb login` see https://docs.wandb.com/huggingface."
            )

        set_seed(self.args.seed)

    def get_train_tfdataset(self) -> tf.data.Dataset:
        """
        Returns the training :class:`~tf.data.Dataset`.
        """
        if self.train_dataset is None:
            raise ValueError("Trainer: training requires a train_dataset.")

        self.total_train_batch_size = self.args.train_batch_size * self.args.gradient_accumulation_steps
        self.num_train_examples = tf.data.experimental.cardinality(self.train_dataset).numpy()

        if self.num_train_examples < 0:
            raise ValueError("The training dataset must have an asserted cardinality")

        ds = (
            self.train_dataset.repeat()
            .shuffle(self.num_train_examples, seed=self.args.seed)
            .batch(self.total_train_batch_size, drop_remainder=self.args.dataloader_drop_last)
            .prefetch(tf.data.experimental.AUTOTUNE)
        )

        return self.args.strategy.experimental_distribute_dataset(ds)

    def get_eval_tfdataset(self, eval_dataset: Optional[tf.data.Dataset] = None) -> tf.data.Dataset:
        """
        Returns the evaluation :class:`~tf.data.Dataset`.

        Args:
            eval_dataset (:class:`~tf.data.Dataset`, `optional`):
                If provided, will override `self.eval_dataset`.
        """
        if eval_dataset is None and self.eval_dataset is None:
            raise ValueError("Trainer: evaluation requires an eval_dataset.")

        eval_dataset = eval_dataset if eval_dataset is not None else self.eval_dataset
        num_examples = tf.data.experimental.cardinality(eval_dataset).numpy()

        if num_examples < 0:
            raise ValueError("The training dataset must have an asserted cardinality")

        approx = math.floor if self.args.dataloader_drop_last else math.ceil
        steps = approx(num_examples / self.args.eval_batch_size)
        ds = (
            eval_dataset.repeat()
            .batch(self.args.eval_batch_size, drop_remainder=self.args.dataloader_drop_last)
            .prefetch(tf.data.experimental.AUTOTUNE)
        )

        return self.args.strategy.experimental_distribute_dataset(ds), steps, num_examples

    def get_test_tfdataset(self, test_dataset: tf.data.Dataset) -> tf.data.Dataset:
        """
        Returns a test :class:`~tf.data.Dataset`.

        Args:
            test_dataset (:class:`~tf.data.Dataset`): The dataset to use.
        """

        num_examples = tf.data.experimental.cardinality(test_dataset).numpy()

        if num_examples < 0:
            raise ValueError("The training dataset must have an asserted cardinality")

        approx = math.floor if self.args.dataloader_drop_last else math.ceil
        steps = approx(num_examples / self.args.eval_batch_size)
        ds = (
            test_dataset.repeat()
            .batch(self.args.eval_batch_size, drop_remainder=self.args.dataloader_drop_last)
            .prefetch(tf.data.experimental.AUTOTUNE)
        )

        return self.args.strategy.experimental_distribute_dataset(ds), steps, num_examples

    def create_optimizer_and_scheduler(
        self, num_training_steps: int,
    ) -> Tuple[tf.keras.optimizers.Optimizer, tf.keras.optimizers.schedules.LearningRateSchedule]:
        """
        Setup the optimizer and the learning rate scheduler.

        We provide a reasonable default that works well. If you want to use something else, you can pass a tuple in the
        TFTrainer's init through :obj:`optimizers`, or override this method in a subclass.
        """
        if not self.optimizer and not self.lr_scheduler:
            self.optimizer, self.lr_scheduler = create_optimizer(
                self.args.learning_rate,
                num_training_steps,
                self.args.warmup_steps,
                adam_beta1=self.args.adam_beta1,
                adam_beta2=self.args.adam_beta2,
                adam_epsilon=self.args.adam_epsilon,
                weight_decay_rate=self.args.weight_decay,
            )

    def _setup_wandb(self):
        """
        Setup the optional Weights & Biases (`wandb`) integration.

        One can override this method to customize the setup if needed.  Find more information at https://docs.wandb.com/huggingface
        You can also override the following environment variables:

        Environment:
            WANDB_PROJECT:
                (Optional): str - "huggingface" by default, set this to a custom string to store results in a different project
            WANDB_DISABLED:
                (Optional): boolean - defaults to false, set to "true" to disable wandb entirely
        """
        logger.info('Automatic Weights & Biases logging enabled, to disable set os.environ["WANDB_DISABLED"] = "true"')
        wandb.init(project=os.getenv("WANDB_PROJECT", "huggingface"), config=vars(self.args))

    def _prediction_loop(
        self,
        dataset: tf.data.Dataset,
        steps: int,
        num_examples: int,
        description: str,
        prediction_loss_only: Optional[bool] = None,
    ) -> PredictionOutput:
        """
        Prediction/evaluation loop, shared by `evaluate()` and `predict()`.

        Works both with or without labels.
        """

        prediction_loss_only = prediction_loss_only if prediction_loss_only is not None else self.prediction_loss_only

        logger.info("***** Running %s *****", description)
        logger.info("  Num examples = %d", num_examples)
        logger.info("  Batch size = %d", self.args.eval_batch_size)

        label_ids: np.ndarray = None
        preds: np.ndarray = None
        self.eval_loss = tf.keras.metrics.Sum()

        # Reset the past mems state at the beginning of the evaluation if necessary.
        if self.args.past_index >= 0:
            self._past = None

        for step, batch in enumerate(dataset):
            logits = self.distributed_test_steps(batch)
            _, labels = batch

            if not prediction_loss_only:
                if isinstance(logits, tuple):
                    logits = logits[0]

                if isinstance(labels, tuple):
                    labels = labels[0]

                if self.args.n_replicas > 1:
                    for val in logits.values:
                        if preds is None:
                            preds = val.numpy()
                        else:
                            preds = np.append(preds, val.numpy(), axis=0)

                    for val in labels.values:
                        if label_ids is None:
                            label_ids = val.numpy()
                        else:
                            label_ids = np.append(label_ids, val.numpy(), axis=0)
                else:
                    if preds is None:
                        preds = logits.numpy()
                    else:
                        preds = np.append(preds, logits.numpy(), axis=0)

                    if label_ids is None:
                        label_ids = labels.numpy()
                    else:
                        label_ids = np.append(label_ids, labels.numpy(), axis=0)

                if step == steps:
                    break

        if self.compute_metrics is not None and preds is not None and label_ids is not None:
            metrics = self.compute_metrics(EvalPrediction(predictions=preds, label_ids=label_ids))
        else:
            metrics = {}

        metrics["eval_loss"] = self.eval_loss.result().numpy() / (steps * self.args.eval_batch_size)

        for key in list(metrics.keys()):
            if not key.startswith("eval_"):
                metrics[f"eval_{key}"] = metrics.pop(key)

        if self.args.past_index and hasattr(self, "_past"):
            # Clean the state at the end of training
            delattr(self, "_past")

        return PredictionOutput(predictions=preds, label_ids=label_ids, metrics=metrics)

    def _log(self, logs: Dict[str, float]) -> None:
        logs["epoch"] = self.epoch_logging

        if self.tb_writer:
            with self.tb_writer.as_default():
                for k, v in logs.items():
                    tf.summary.scalar(k, v, step=self.global_step)
            self.tb_writer.flush()

        if is_wandb_available():
            wandb.log(logs, step=self.global_step)

        output = {**logs, **{"step": self.global_step}}

        logger.info(output)

    def evaluate(self, eval_dataset: Optional[tf.data.Dataset] = None) -> Dict[str, float]:
        """
        Run evaluation and returns metrics.

        The calling script will be responsible for providing a method to compute metrics, as they are
        task-dependent (pass it to the init :obj:`compute_metrics` argument).

        Args:
            eval_dataset (:class:`~tf.data.Dataset`, `optional`):
                Pass a dataset if you wish to override :obj:`self.eval_dataset`.
        Returns:
            A dictionary containing the evaluation loss and the potential metrics computed from the predictions.
        """
        eval_ds, steps, num_examples = self.get_eval_tfdataset(eval_dataset)

        output = self._prediction_loop(eval_ds, steps, num_examples, description="Evaluation")

        logs = {**output.metrics}
        logs["epoch"] = self.epoch_logging

        self._log(logs)

        return output.metrics

    def test_step(self, features, labels):
        per_example_loss, logits = self._run_model(features, labels, False)

        self.eval_loss.update_state(per_example_loss)

        return logits

    @tf.function
    def distributed_test_steps(self, batch):
        logits = self.args.strategy.run(self.test_step, batch)

        return logits

    def train(self) -> None:
        """
        Train method to train the model.
        """
        train_ds = self.get_train_tfdataset()

        if self.args.debug:
            tf.summary.trace_on(graph=True, profiler=True)

        self.gradient_accumulator.reset()

        if self.args.max_steps > 0:
            t_total = self.args.max_steps
            self.steps_per_epoch = self.args.max_steps
        else:
            approx = math.floor if self.args.dataloader_drop_last else math.ceil
            self.steps_per_epoch = approx(self.num_train_examples / self.total_train_batch_size)
            t_total = self.steps_per_epoch * self.args.num_train_epochs

        with self.args.strategy.scope():
            self.create_optimizer_and_scheduler(num_training_steps=t_total)
            iterations = self.optimizer.iterations
            self.global_step = iterations.numpy()
            folder = os.path.join(self.args.output_dir, PREFIX_CHECKPOINT_DIR)
            ckpt = tf.train.Checkpoint(optimizer=self.optimizer, model=self.model)
            self.model.ckpt_manager = tf.train.CheckpointManager(ckpt, folder, max_to_keep=self.args.save_total_limit)

            if self.model.ckpt_manager.latest_checkpoint:
                epochs_trained = self.global_step // (self.num_train_examples // self.args.gradient_accumulation_steps)
                steps_trained_in_current_epoch = self.global_step % (
                    self.num_train_examples // self.args.gradient_accumulation_steps
                )

                logger.info("  Continuing training from checkpoint, will skip to saved global_step")
                logger.info("  Continuing training from epoch %d", epochs_trained)
                logger.info("  Continuing training from global step %d", self.global_step)
                logger.info("  Will skip the first %d steps in the first epoch", steps_trained_in_current_epoch)
                logger.info(
                    "Checkpoint file %s found and restoring from checkpoint", self.model.ckpt_manager.latest_checkpoint
                )

                ckpt.restore(self.model.ckpt_manager.latest_checkpoint).expect_partial()
            else:
                epochs_trained = 1

            tf.summary.experimental.set_step(iterations)

            epochs = 1 if self.args.max_steps > 0 else self.args.num_train_epochs

            if self.args.fp16:
                policy = tf.keras.mixed_precision.experimental.Policy("mixed_float16")
                tf.keras.mixed_precision.experimental.set_policy(policy)

            with self.tb_writer.as_default():
                tf.summary.text("args", self.args.to_json_string())

            self.tb_writer.flush()

            logger.info("***** Running training *****")
            logger.info("  Num examples = %d", self.num_train_examples)
            logger.info("  Num Epochs = %d", epochs)
            logger.info("  Instantaneous batch size per device = %d", self.args.per_device_train_batch_size)
            logger.info(
                "  Total train batch size (w. parallel, distributed & accumulation) = %d", self.total_train_batch_size
            )
            logger.info("  Gradient Accumulation steps = %d", self.args.gradient_accumulation_steps)
            logger.info("  Steps per epoch = %d", self.steps_per_epoch)
            logger.info("  Total optimization steps = %d", t_total)

            self.train_loss = tf.keras.metrics.Sum()
            start_time = datetime.datetime.now()

            for epoch_iter in range(epochs_trained, int(epochs + 1)):
                # Reset the past mems state at the beginning of each epoch if necessary.
                if self.args.past_index >= 0:
                    self._past = None

                for step, batch in enumerate(train_ds):
                    self.global_step = iterations.numpy()
                    self.epoch_logging = epoch_iter - 1 + (step + 1) / self.steps_per_epoch

                    self.distributed_training_steps(batch)

                    training_loss = self.train_loss.result() / ((step + 1) * self.total_train_batch_size)

                    if self.args.debug:
                        logs = {}
                        logs["loss"] = training_loss.numpy()
                        logs["epoch"] = self.epoch_logging

                        self._log(logs)

                    if self.global_step == 1 and self.args.debug:
                        with self.tb_writer.as_default():
                            tf.summary.trace_export(
                                name="training", step=self.global_step, profiler_outdir=self.args.logging_dir
                            )

                    if (
                        self.global_step > 0
                        and self.args.evaluate_during_training
                        and self.global_step % self.args.eval_steps == 0
                    ):
                        self.evaluate()

                    if (self.global_step > 0 and self.global_step % self.args.logging_steps == 0) or (
                        self.global_step == 1 and self.args.logging_first_step
                    ):
                        logs = {}
                        logs["loss"] = training_loss.numpy()
                        logs["learning_rate"] = self.lr_scheduler(self.global_step).numpy()
                        logs["epoch"] = self.epoch_logging

                        self._log(logs)

                    if self.global_step > 0 and self.global_step % self.args.save_steps == 0:
                        ckpt_save_path = self.model.ckpt_manager.save()

                        logger.info("Saving checkpoint for step {} at {}".format(self.global_step, ckpt_save_path))

                    if self.global_step > 0 and self.global_step % self.steps_per_epoch == 0:
                        break

                self.train_loss.reset_states()

            end_time = datetime.datetime.now()

            logger.info("Training took: {}".format(str(end_time - start_time)))

        if self.args.past_index and hasattr(self, "_past"):
            # Clean the state at the end of training
            delattr(self, "_past")

    def training_step(self, features, labels):
        per_example_loss, _ = self._run_model(features, labels, True)
        scaled_loss = per_example_loss / self.total_train_batch_size
        gradients = tf.gradients(scaled_loss, self.model.trainable_variables)
        gradients = [
            g if g is not None else tf.zeros_like(v) for g, v in zip(gradients, self.model.trainable_variables)
        ]

        if self.args.gradient_accumulation_steps > 1:
            self.gradient_accumulator(gradients)

        self.train_loss.update_state(per_example_loss)

        if self.args.gradient_accumulation_steps == 1:
            return gradients

    def apply_gradients(self, features, labels):
        if self.args.gradient_accumulation_steps == 1:
            gradients = self.training_step(features, labels)

            self.optimizer.apply_gradients(list(zip(gradients, self.model.trainable_variables)))
        else:
            for _ in tf.range(self.args.gradient_accumulation_steps):
                reduced_features = features[: self.args.train_batch_size / self.args.n_replicas]
                reduced_labels = labels[: self.args.train_batch_size / self.args.n_replicas]

                self.training_step(reduced_features, reduced_labels)

                features = tf.concat(
                    [features[self.args.train_batch_size / self.args.n_replicas :], reduced_features], axis=0
                )

            gradients = self.gradient_accumulator.gradients
            gradients = [
                (tf.clip_by_value(grad, -self.args.max_grad_norm, self.args.max_grad_norm)) for grad in gradients
            ]

            self.optimizer.apply_gradients(list(zip(gradients, self.model.trainable_variables)))
            self.gradient_accumulator.reset()

    @tf.function
    def distributed_training_steps(self, batch):
        with self.args.strategy.scope():
            self.args.strategy.run(self.apply_gradients, batch)

    def _run_model(self, features, labels, training):
        """
        Computes the loss of the given features and labels pair.
        Args:
          features: the batched features.
          labels: the batched labels.
          training: run the model in training mode or not
        """
        if self.args.past_index >= 0 and getattr(self, "_past", None) is not None:
            features["mems"] = self._past

        if isinstance(labels, (dict)):
            outputs = self.model(features, training=training, **labels)[:2]
        else:
            outputs = self.model(features, labels=labels, training=training)[:2]

        loss, logits = outputs[:2]

        if self.args.past_index >= 0:
            self._past = outputs[self.args.past_index]

        return loss, logits

    def predict(self, test_dataset: tf.data.Dataset) -> PredictionOutput:
        """
        Run prediction and returns predictions and potential metrics.

        Depending on the dataset and your use case, your test dataset may contain labels.
        In that case, this method will also return metrics, like in :obj:`evaluate()`.

        Args:
            test_dataset (:class:`~tf.data.Dataset`):
                Dataset to run the predictions on.
        Returns:
            `NamedTuple`:
            predictions (:obj:`np.ndarray`):
                The predictions on :obj:`test_dataset`.
            label_ids (:obj:`np.ndarray`, `optional`):
                The labels (if the dataset contained some).
            metrics (:obj:`Dict[str, float]`, `optional`):
                The potential dictionary of metrics (if the dataset contained labels).
        """
        test_ds, steps, num_examples = self.get_test_tfdataset(test_dataset)

        return self._prediction_loop(test_ds, steps, num_examples, description="Prediction")

    def save_model(self, output_dir: Optional[str] = None):
        """
        Will save the model, so you can reload it using :obj:`from_pretrained()`.
        """
        output_dir = output_dir if output_dir is not None else self.args.output_dir

        logger.info("Saving model in {}".format(output_dir))

        if not isinstance(self.model, TFPreTrainedModel):
            raise ValueError("Trainer.model appears to not be a PreTrainedModel")

        self.model.save_pretrained(output_dir)
