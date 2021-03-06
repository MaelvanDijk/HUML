from __future__ import annotations

import random
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple, Union

import numpy as np
import tensorflow as tf
import torch
from loguru import logger
from torch.nn.utils.rnn import pad_sequence
from tqdm import tqdm

Tensor = torch.Tensor


def walk_dir(path: Path) -> Iterator:
    """loops recursively through a folder

    Args:
        path (Path): folder to loop trough. If a directory
            is encountered, loop through that recursively.

    Yields:
        Generator: all paths in a folder and subdirs.
    """

    for p in Path(path).iterdir():
        if p.is_dir():
            yield from walk_dir(p)
            continue
        # resolve works like .absolute(), but it removes the "../.." parts
        # of the location, so it is cleaner
        yield p.resolve()


def iter_valid_paths(path: Path, formats: List[str]) -> Tuple[Iterator, List[str]]:
    """
    Gets all paths in folders and subfolders
    strips the classnames assuming that the subfolders are the classnames
    Keeps only paths with the right suffix


    Args:
        path (Path): image folder
        formats (List[str]): suffices to keep.

    Returns:
        Tuple[Iterator, List[str]]: _description_
    """
    # gets all files in folder and subfolders
    walk = walk_dir(path)
    # retrieves foldernames as classnames
    class_names = [subdir.name for subdir in path.iterdir() if subdir.is_dir()]
    # keeps only specified formats
    paths = (path for path in walk if path.suffix in formats)
    return paths, class_names


class Dataloader:
    """Point the dataloader to a directory.
    It will load all files, taking the subfolders as classes.
    Only files in the formatslist are kept.
    The .datagenerator returns an Iterator of batched images and labels
    """

    def __init__(
        self,
        path: Path,
        split: float,
        formats: List[str] = [".png", ".jpg"],
    ) -> None:
        """
        Initializes the class

        Args:
            path (Path): location of the images
            formats (List[str], optional): Formats to keep. Defaults to [".png", ".jpg"]
        """

        # get all paths
        self.paths, self.class_names = iter_valid_paths(path, formats)
        # make a dictionary mapping class names to an integer
        self.class_dict: Dict[str, int] = {
            k: v for k, v in zip(self.class_names, range(len(self.class_names)))
        }
        # unpack generator
        self.valid_files = [*self.paths]
        self.data_size = len(self.valid_files)
        self.index_list = [*range(self.data_size)]

        random.shuffle(self.index_list)

        n_train = int(self.data_size * split)
        self.train = self.index_list[:n_train]
        self.test = self.index_list[n_train:]

    def __len__(self) -> int:
        return len(self.valid_files)

    def data_generator(
        self,
        batch_size: int,
        image_size: Tuple[int, int],
        channels: int,
        channel_first: bool,
        mode: str,
        shuffle: bool = True,
        transforms: Optional[Callable] = None,
    ) -> Iterator:
        """
        Builds batches of images

        Args:
            batch_size (int): _description_
            image_size (Tuple[int, int]): _description_
            channels (int): _description_
            shuffle (bool, optional): _description_. Defaults to True.

        Yields:
            Iterator: _description_
        """
        if mode == "train":
            data_size = len(self.train)
            index_list = self.train
        else:
            data_size = len(self.test)
            index_list = self.test

        index = 0
        while True:
            # prepare empty matrices
            X = torch.zeros(  # noqa: N806
                (batch_size, channels, image_size[0], image_size[1])
            )  # noqa: N806
            Y = torch.zeros(batch_size, dtype=torch.long)  # noqa: N806

            for i in range(batch_size):
                if index >= data_size:
                    index = 0
                    if shuffle:
                        random.shuffle(index_list)
                # get path
                file = self.valid_files[index_list[index]]
                # get image from disk
                if transforms is not None:
                    img = self.load_image(file, image_size, channels)
                    X[i] = transforms(img)
                else:
                    X[i] = torch.tensor(self.load_image(file, image_size, channels))
                # map parent directory name to integer
                Y[i] = self.class_dict[file.parent.name]
                index += 1

            if not channel_first:
                X = torch.permute(X, (0, 2, 3, 1))  # noqa N806

            yield ((X, Y))

    def load_image(
        self, path: Path, image_size: Tuple[int, int], channels: int
    ) -> np.ndarray:
        # load file
        img_ = tf.io.read_file(str(path))
        # decode as image
        img = tf.image.decode_image(img_, channels=channels)
        # resize with bilinear algorithm
        img_resize = tf.image.resize(img, image_size, method="bilinear")
        # add correct shape with channels-last convention
        img_resize.set_shape((image_size[0], image_size[1], channels))
        # cast to numpy
        return img_resize.numpy().astype(np.uint8)


def clean_dir(dir: Union[str, Path]) -> None:
    dir = Path(dir)
    if dir.exists():
        logger.info(f"Clean out {dir}")
        shutil.rmtree(dir)
    else:
        dir.mkdir(parents=True)


def window(x: Tensor, n_time: int) -> Tensor:
    """
    Generates and index that can be used to window a timeseries.
    E.g. the single series [0, 1, 2, 3, 4, 5] can be windowed into 4 timeseries with
    length 3 like this:

    [0, 1, 2]
    [1, 2, 3]
    [2, 3, 4]
    [3, 4, 5]

    We now can feed 4 different timeseries into the model, instead of 1, all
    with the same length.
    """
    n_window = len(x) - n_time + 1
    time = torch.arange(0, n_time).reshape(1, -1)
    window = torch.arange(0, n_window).reshape(-1, 1)
    idx = time + window
    return idx


def dir_add_timestamp(log_dir: Optional[Path] = None) -> Path:
    if log_dir is None:
        log_dir = Path(".")
    log_dir = Path(log_dir)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M")
    log_dir = log_dir / timestamp
    logger.info(f"Logging to {log_dir}")
    if not log_dir.exists():
        log_dir.mkdir(parents=True)
    return log_dir


class BaseDataset:
    """The main responsibility of the Dataset class is to load the data from disk
    and to offer a __len__ method and a __getitem__ method
    """

    def __init__(self, paths: List[Path]) -> None:
        self.paths = paths
        random.shuffle(self.paths)
        self.dataset: List = []
        self.process_data()

    def process_data(self) -> None:
        raise NotImplementedError

    def __len__(self) -> int:
        return len(self.dataset)

    def __getitem__(self, idx: int) -> Tuple:
        return self.dataset[idx]


class TSDataset(BaseDataset):
    """This assume a txt file with numeric data
    Dropping the first columns is hardcoded
    y label is name-1, because the names start with 1

    Args:
        BaseDataset (_type_): _description_
    """

    def process_data(self) -> None:
        for file in tqdm(self.paths):
            x_ = np.genfromtxt(file)[:, 3:]
            x = torch.tensor(x_).type(torch.float32)
            y = torch.tensor(int(file.parent.name) - 1)
            self.dataset.append((x, y))


class TextDataset(BaseDataset):
    """This assumes textual data, one line per file

    Args:
        BaseDataset (_type_): _description_
    """

    def process_data(self) -> None:
        for file in tqdm(self.paths):
            with open(file) as f:
                x = f.readline()
            y = file.parent.name
            self.dataset.append((x, y))


class BaseDataIterator:
    """This iterator will consume all data and stop automatically.
    The dataset should have a:
        __len__ method
        __getitem__ method

    """

    def __init__(self, dataset: BaseDataset, batchsize: int) -> None:
        self.dataset = dataset
        self.batchsize = batchsize

    def __len__(self) -> int:
        return int(len(self.dataset) / self.batchsize)

    def __iter__(self) -> BaseDataIterator:
        self.index = 0
        self.index_list = torch.randperm(len(self.dataset))
        return self

    def batchloop(self) -> Tuple[List, List]:
        X = []  # noqa N806
        Y = []  # noqa N806
        for _ in range(self.batchsize):
            x, y = self.dataset[int(self.index_list[self.index])]
            X.append(x)
            Y.append(y)
            self.index += 1
        return X, Y

    def __next__(self) -> Tuple[Tensor, Tensor]:
        if self.index <= (len(self.dataset) - self.batchsize):
            X, Y = self.batchloop()  # noqa N806
            return torch.tensor(X), torch.tensor(Y)
        else:
            raise StopIteration


class PaddedDatagenerator(BaseDataIterator):
    """Iterator with additional padding of X

    Args:
        BaseDataIterator (_type_): _description_
    """

    def __init__(self, dataset: BaseDataset, batchsize: int) -> None:
        super().__init__(dataset, batchsize)

    def __next__(self) -> Tuple[Tensor, Tensor]:
        if self.index <= (len(self.dataset) - self.batchsize):
            X, Y = self.batchloop()  # noqa N806
            X_ = pad_sequence(X, batch_first=True, padding_value=0)  # noqa N806
            return X_, torch.tensor(Y)
        else:
            raise StopIteration


class BaseDatastreamer:
    """This datastreamer wil never stop
    The dataset should have a:
        __len__ method
        __getitem__ method

    """

    def __init__(
        self,
        dataset: BaseDataset,
        batchsize: int,
        preprocessor: Optional[Callable] = None,
    ) -> None:
        self.dataset = dataset
        self.batchsize = batchsize
        self.preprocessor = preprocessor
        self.size = len(self.dataset)
        self.reset_index()

    def __len__(self) -> int:
        return int(len(self.dataset) / self.batchsize)

    def reset_index(self) -> None:
        self.index_list = np.random.permutation(self.size)
        self.index = 0

    def batchloop(self) -> Sequence[Tuple]:
        batch = []
        for _ in range(self.batchsize):
            x, y = self.dataset[int(self.index_list[self.index])]
            batch.append((x, y))
            self.index += 1
        return batch

    def stream(self) -> Iterator:
        while True:
            if self.index > (self.size - self.batchsize):
                self.reset_index()
            batch = self.batchloop()
            if self.preprocessor is not None:
                X, Y = self.preprocessor(batch)  # noqa N806
            else:
                X, Y = zip(*batch)  # noqa N806
            yield X, Y
