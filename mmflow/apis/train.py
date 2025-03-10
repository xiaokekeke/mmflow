# Copyright (c) OpenMMLab. All rights reserved.
import random
import warnings
from typing import Optional, Sequence, Union

import numpy as np
import torch
from mmcv.parallel import MMDataParallel, MMDistributedDataParallel
from mmcv.runner import (HOOKS, Fp16OptimizerHook, OptimizerHook,
                         build_optimizer, build_runner)
from mmcv.utils import Config, build_from_cfg

from mmflow.core import DistEvalHook, EvalHook
from mmflow.datasets import build_dataloader, build_dataset
from mmflow.utils import get_root_logger

Module = torch.nn.Module
Dataset = torch.utils.data.Dataset


def set_random_seed(seed: int, deterministic: bool = False) -> None:
    """Set random seed.

    Args:
        seed (int): Seed to be used.
        deterministic (bool): Whether to set the deterministic option for
            CUDNN backend, i.e., set `torch.backends.cudnn.deterministic`
            to True and `torch.backends.cudnn.benchmark` to False.
            Default: False.
    """
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    if deterministic:
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False


def train_model(model: Module,
                dataset: Union[Sequence[Dataset], Dataset],
                cfg: Config,
                distributed: bool = False,
                validate: bool = False,
                timestamp: Optional[str] = None,
                meta: Optional[dict] = None) -> None:
    """Training model.

    Args:
        model (Module): The optical flow estimator model.
        dataset (list, Dataset): The training datasets.
        cfg (Config): The training config.
        distributed (bool): Whether training is distributed. Defaults to False.
        validate (bool): Whether validate model when training.
            Defaults to False.
        timestamp (str, optional): The string of time stamp. Defaults to None.
        meta (dict, optional): Meta dict to record some important
            information such as environment info and seed, which will be
            logged. Defaults to None.
    """
    logger = get_root_logger(cfg.log_level)

    # prepare data loaders
    dataset = dataset if isinstance(dataset, (list, tuple)) else [dataset]
    data_loaders = [
        build_dataloader(
            ds,
            # cfg.gpus will be ignored if distributed
            num_gpus=len(cfg.gpu_ids),
            dist=distributed,
            seed=cfg.seed,
            **cfg.data.train_dataloader) for ds in dataset
    ]

    # put model on gpus
    if distributed:
        find_unused_parameters = cfg.get('find_unused_parameters', False)
        # Sets the `find_unused_parameters` parameter in
        # torch.nn.parallel.DistributedDataParallel
        model = MMDistributedDataParallel(
            model.cuda(),
            device_ids=[torch.cuda.current_device()],
            broadcast_buffers=False,
            find_unused_parameters=find_unused_parameters)
    else:
        model = MMDataParallel(
            model.cuda(cfg.gpu_ids[0]), device_ids=cfg.gpu_ids)

    # build runner
    optimizer = build_optimizer(model, cfg.optimizer)
    if cfg.get('runner') is None:
        cfg.runner = {'type': 'IterBasedRunner', 'max_iters': cfg.total_iters}
        warnings.warn(
            'config is now expected to have a `runner` section, '
            'please set `runner` in your config.', UserWarning)

    runner = build_runner(
        cfg.runner,
        default_args=dict(
            model=model,
            batch_processor=None,
            optimizer=optimizer,
            work_dir=cfg.work_dir,
            logger=logger,
            meta=meta))

    # an ugly walkaround to make the .log and .log.json filenames the same
    runner.timestamp = timestamp

    # fp16 setting
    fp16_cfg = cfg.get('fp16', None)
    if fp16_cfg is not None:
        optimizer_config = Fp16OptimizerHook(
            **cfg.optimizer_config, **fp16_cfg, distributed=distributed)
    elif distributed and 'type' not in cfg.optimizer_config:
        optimizer_config = OptimizerHook(**cfg.optimizer_config)
    else:
        optimizer_config = cfg.optimizer_config

    # register hooks
    runner.register_training_hooks(cfg.lr_config, optimizer_config,
                                   cfg.checkpoint_config, cfg.log_config,
                                   cfg.get('momentum_config', None))

    # register eval hooks
    if validate:
        separate_eval = cfg.data.val.get('separate_eval', False)
        if separate_eval:
            val_dataset = [
                build_dataset(dataset) for dataset in cfg.data.val.datasets
            ]
            val_dataloader = [
                build_dataloader(
                    _val_dataset, **cfg.data.val_dataloader, dist=distributed)
                for _val_dataset in val_dataset
            ]
            val_dataset_name = [ds.dataset_name for ds in val_dataset]
        else:

            val_dataset = build_dataset(cfg.data.val, dict(test_mode=True))
            val_dataloader = build_dataloader(
                val_dataset, **cfg.data.val_dataloader, dist=distributed)
            val_dataset_name = val_dataset.dataset_name

        eval_cfg = cfg.get('evaluation', {})
        eval_cfg['by_epoch'] = cfg.runner['type'] != 'IterBasedRunner'

        eval_hook = DistEvalHook if distributed else EvalHook
        # In this PR (https://github.com/open-mmlab/mmcv/pull/1193), the
        # priority of IterTimerHook has been modified from 'NORMAL'
        # to 'LOW'.
        runner.register_hook(
            eval_hook(
                val_dataloader, **eval_cfg, dataset_name=val_dataset_name),
            priority='LOW')

    # user-defined hooks
    if cfg.get('custom_hooks', None):
        custom_hooks = cfg.custom_hooks
        assert isinstance(custom_hooks, list), \
            f'custom_hooks expect list type, but got {type(custom_hooks)}'
        for hook_cfg in cfg.custom_hooks:
            assert isinstance(hook_cfg, dict), \
                'Each item in custom_hooks expects dict type, but got ' \
                f'{type(hook_cfg)}'
            hook_cfg = hook_cfg.copy()
            priority = hook_cfg.pop('priority', 'NORMAL')
            hook = build_from_cfg(hook_cfg, HOOKS)
            runner.register_hook(hook, priority=priority)

    if cfg.resume_from:
        runner.resume(cfg.resume_from)
    elif cfg.load_from:
        runner.load_checkpoint(cfg.load_from)
    runner.run(data_loaders, cfg.workflow)
