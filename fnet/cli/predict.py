"""Generates predictions from a model."""


from typing import Callable, Dict, Optional
import argparse
import json
import os

import numpy as np
import pandas as pd
import tifffile
import torch

from fnet.data.tiffdataset import TiffDataset
from fnet.models import load_model
from fnet.transforms import norm_around_center
from fnet.utils.general_utils import files_from_dir
from fnet.utils.general_utils import retry_if_oserror
from fnet.utils.general_utils import str_to_object


def get_dataset(args: argparse.Namespace) -> torch.utils.data.Dataset:
    """Returns dataset.

    Returns
    -------
    torch.utils.data.Dataset
        Dataset object.

    """
    if sum([args.dataset is not None, args.path_tif is not None]) != 1:
        raise ValueError('Must specify one input source type')
    if args.dataset is not None:
        ds_fn = str_to_object(args.dataset)
        if not isinstance(ds_fn, Callable):
            raise ValueError(f'{args.dataset} must be callable')
        return ds_fn(**args.dataset_kwargs)
    if args.path_tif is not None:
        if not os.path.exists(args.path_tif):
            raise ValueError(f'Path does not exists: {args.path_tif}')
        paths_tif = [args.path_tif]
        if os.path.isdir(args.path_tif):
            paths_tif = files_from_dir(args.path_tif)
        ds = TiffDataset(
            dataframe=pd.DataFrame(
                {'path_bf': paths_tif, 'path_target': None}
            ),
            transform_signal=[norm_around_center],
            transform_target=[norm_around_center],
            col_signal='path_bf',
        )
        return ds
    raise NotImplementedError


def save_tif(fname: str, ar: np.ndarray, path_root: str) -> str:
    """Saves a tif and returns tif save path relative to root save directory.

    Image will be stored at: 'path_root/tifs/fname'

    Parameters
    ----------
    fname
        Basename of save path.
    ar
        Array to be saved as tif.
    path_root
        Root directory of save path.

    Returns
    -------
    str
        Save path relative to root directory.

    """
    path_tif_dir = os.path.join(path_root, 'tifs')
    if not os.path.exists(path_tif_dir):
        os.makedirs(path_tif_dir)
        print('Created:', path_tif_dir)
    path_save = os.path.join(path_tif_dir, fname)
    tifffile.imsave(path_save, ar, compress=2)
    print('Saved:', path_save)
    return os.path.relpath(path_save, path_root)


def parse_model(model_str: str) -> Dict:
    """Parse model definition string into dictionary."""
    model_def = {}
    parts = model_str.split(':')
    if len(parts) > 2:
        raise ValueError('Multiple ":" in specified model')
    name = os.path.basename(parts[0])
    options = []
    if len(parts) == 2:
        options.extend(parts[1].split(','))
    model_def['path'] = parts[0]
    model_def['options'] = options
    model_def['name'] = '.'.join([name] + options)
    return model_def


def save_csv(path_csv, df: pd.DataFrame) -> None:
    """Saves dataframe as csv and merges with existing csv if necessary."""
    if os.path.exists(path_csv):
        df_old = pd.read_csv(path_csv)
        col_index = df_old.columns[0]  # Assumes first col is index col
        df_old = df_old.set_index(col_index)
        df = df.combine_first(df_old)
    df = df.sort_index(axis=1)
    retry_if_oserror(df.to_csv)(path_csv)
    print('Saved:', path_csv)


def save_args_as_json(path_save_dir: str, args: argparse.Namespace) -> None:
    """Saves script arguments as json in save directory.

    By default, tries to save arguments are predict_options.json within the
    save directory. If that file already exists, appends a digit to uniquify
    the save path.

    Parameters
    ----------
    path_save_dir
        Save directory
    args
        Script arguments.

    """
    path_json = os.path.join(path_save_dir, 'predict_options.json')
    while os.path.exists(path_json):
        number = path_json.split('.')[-2]
        if not number.isdigit():
            number = '-1'
        number = str(int(number) + 1)
        path_json = os.path.join(
            path_save_dir, '.'.join(['predict_options', number, 'json'])
        )
    with open(path_json, 'w') as fo:
        json.dump(vars(args), fo, indent=4, sort_keys=True)
        print('Saved:', path_json)


def add_parser_arguments(parser) -> None:
    """Add training script arguments to parser."""
    parser.add_argument('path_model_dir', nargs='+', help='path(s) to model directory')
    parser.add_argument('--add_sigmoid', action='store_true', help='pass model output through sigmoid layer')
    parser.add_argument('--dataset', help='dataset name')
    parser.add_argument('--dataset_kwargs', help='dataset kwargs')
    parser.add_argument('--gpu_ids', type=int, default=0, help='GPU ID')
    parser.add_argument('--idx_sel', nargs='+', type=int, help='specify dataset indices')
    parser.add_argument('--metric', default='fnet.metrics.corr_coef', help='evaluation metric')
    parser.add_argument('--n_images', type=int, default=-1, help='max number of images to test')
    parser.add_argument('--no_prediction', action='store_true', help='set to not save prediction image')
    parser.add_argument('--no_signal', action='store_true', help='set to not save signal image')
    parser.add_argument('--no_target', action='store_true', help='set to not save target image')
    parser.add_argument('--path_save_dir', default='predictions', help='path to output root directory')
    parser.add_argument('--path_tif', help='path(s) to input tif(s)')


def main(args: Optional[argparse.Namespace] = None) -> None:
    """Predicts using model."""
    if args is None:
        parser = argparse.ArgumentParser()
        add_parser_arguments(parser)
        args = parser.parse_args()
    metric = str_to_object(args.metric)
    dataset = get_dataset(args)
    index_name = dataset.df.index.name or 'index'
    if not os.path.exists(args.path_save_dir):
        os.makedirs(args.path_save_dir)
    entries = []
    model = None
    indices = (
        args.idx_sel if args.idx_sel is not None else dataset.df.index
    )[:args.n_images if args.n_images > 0 else None]
    for count, idx in enumerate(indices, 1):
        print(f'Processing: {idx:3d} ({count}/{len(indices)})')
        entry = {}
        entry[index_name] = idx
        data = dataset.loc[idx]
        signal = data[0]
        target = data[1] if len(data) > 1 else None
        if not args.no_signal:
            entry['path_signal'] = save_tif(
                f'{idx}_signal.tif', signal.numpy()[0, ], args.path_save_dir
            )
        if not args.no_target and target is not None:
            entry['path_target'] = save_tif(
                f'{idx}_target.tif', target.numpy()[0, ], args.path_save_dir
            )
        for path_model_dir in args.path_model_dir:
            if model is None or len(args.path_model_dir) > 1:
                model_def = parse_model(path_model_dir)
                model = load_model(model_def['path'], no_optim=True)
                model.to_gpu(args.gpu_ids)
                print('Predicting with:', model_def['name'])
            prediction = model.predict_piecewise(
                signal,
                tta=('no_tta' not in model_def['options']),
            )
            if args.add_sigmoid:
                prediction = torch.nn.functional.sigmoid(prediction)
            evaluation = metric(target, prediction)
            entry[args.metric + f'.{model_def["name"]}'] = evaluation
            if not args.no_prediction and prediction is not None:
                for idx_c in range(prediction.size()[0]):
                    tag = f'prediction_c{idx_c}.{model_def["name"]}'
                    pred_c = prediction.numpy()[idx_c, ]
                    entry[f'path_{tag}'] = save_tif(
                        f'{idx}_{tag}.tif', pred_c, args.path_save_dir
                    )
        entries.append(entry)
        if ((count % 8) == 0) or (idx == indices[-1]):
            save_csv(
                os.path.join(args.path_save_dir, 'predictions.csv'),
                pd.DataFrame(entries).set_index(index_name),
            )
    save_args_as_json(args.path_save_dir, args)


if __name__ == '__main__':
    main()
