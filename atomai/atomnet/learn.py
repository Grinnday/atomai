"""
learn.py
========

Module for training fully convolutional neural network (FCNN)
for atom/defect/particle finding as well as an ensemble of FCNNs.

Created by Maxim Ziatdinov (email: maxim.ziatdinov@ai4microscopy.com)

"""


import copy
import os
import warnings
from collections import OrderedDict
from typing import Dict, List, Tuple, Type, Union, Callable

import numpy as np
import torch
from atomai import losses_metrics
from atomai.nets import dilnet, dilUnet
from atomai.transforms import datatransform, unsqueeze_channels
from atomai.utils import (gpu_usage_map, plot_losses, set_train_rng,
                          preprocess_training_data, sample_weights)
from sklearn.model_selection import train_test_split

warnings.filterwarnings("ignore", module="torch.nn.functional")

training_data_types = Union[np.ndarray, List[np.ndarray], Dict[int, np.ndarray]]
ensemble_out = Tuple[Dict[int, Dict[str, torch.Tensor]], Type[torch.nn.Module]]


class trainer:
    """
    Class for training a fully convolutional neural network
    for semantic segmentation of noisy experimental data

    Args:
        X_train (list or dict or 4D numpy array):
            Training images in the form of list/dictionary of
            small 4D numpy arrays (batches) or larger 4D numpy array
            representing all the training images. For dictionary with N batches,
            the keys must be 0, 1, 2, ... *N*. Both small and large 4D numpy arrays
            represent 3D images :math:`(height \\times width \\times 1)` stacked
            along the zeroth ("batch") dimension.
        y_train (list or dict or 4D numpy array):
            Training labels (aka ground truth aka masks) in the form of
            list/dictionary of small 3D (binary classification) or 4D (multiclass)
            numpy arrays or larger 4D (binary) / 3D (multiclass) numpy array
            containing all the training labels.
            For dictionary with N batches, the keys must be 0, 1, 2, ... *N*.
            Both small and large numpy arrays are 3D (binary) / 2D (multiclass) images
            stacked along the zeroth ("batch") dimension. The reason why in the
            multiclass case the images have 4 dimensions while the labels have only 3 dimensions
            is because of how the cross-entropy loss is calculated in PyTorch
            (see https://pytorch.org/docs/stable/nn.html#nllloss).
        X_test (list or dict or 4D numpy array):
            Test images in the form of list/dictionary of
            small 4D numpy arrays (batches) or larger 4D numpy array
            representing all the test images. For dictionary with N batches,
            the keys must be 0, 1, 2, ... *N*. Both small and large 4D numpy arrays
            represent 3D images :math:`(height \\times width \\times 1)` stacked
            along the zeroth ("batch") dimension.
        y_test (list or dict or 4D numpy array):
            Test labels (aka ground truth aka masks) in the form of
            list/dictionary of small 3D (binary classification) or 4D (multiclass)
            numpy arrays or larger 4D (binary) / 3D (multiclass) numpy array
            containing all the test labels.
            For dictionary with N batches, the keys must be 0, 1, 2, ... *N*.
            Both small and large numpy arrays are 3D (binary) / 2D (multiclass) images
            stacked along the zeroth ("batch") dimenstion.
        training_cycles (int):
            Number of training 'epochs' (1 epoch == 1 batch)
        model (str):
            Type of model to train: 'dilUnet' or 'dilnet' (Default: 'dilUnet').
            See atomai.nets for more details. One can also pass a custom fully
            convolutional neural network model.
        IoU (bool):
            Compute and show mean Intersection over Union for each batch/iteration
            (Default: False)
        seed (int):
            Deterministic mode for model training (Default: 1)
        batch_seed (int):
            Separate seed for generating a sequence of batches
            for training/testing. Equal to 'seed' if set to None (default)
        **batch_size (int):
            Size of training and test batches
        **use_batchnorm (bool):
            Apply batch normalization after each convolutional layer
            (Default: True)
        **use_dropouts (bool):
            Apply dropouts in the three inner blocks in the middle of a network
            (Default: False)
        **loss (str):
            Type of loss for model training ('ce', 'dice' or 'focal')
            (Default: 'ce')
        **upsampling_mode (str):
            "bilinear" or "nearest" upsampling method (Default: "bilinear")
        **nb_filters (int):
            Number of convolutional filters in the first convolutional block
            (this number doubles in the consequtive block(s),
            see definition of dilUnet and dilnet models for details)
        **with_dilation (bool):
            Use dilated convolutions in the bottleneck of dilUnet
            (Default: True)
        **layers (list):
            List with a number of layers in each block.
            For U-Net the first 4 elements in the list
            are used to determine the number of layers
            in each block of the encoder (including bottleneck layer),
            and the number of layers in the decoder  is chosen accordingly
            (to maintain symmetry between encoder and decoder)
        **swag (bool): Performs diagonal Gaussian subspace samling at the end
            of training (does not work if batch normalization is ON!)
        **print_loss (int):
            Prints loss every *n*-th epoch
        **savedir (str):
            Directory to automatically save intermediate and final weights
        **savename (str):
            Filename for model weights
            (appended with "_test_weights_best.pt" and "_weights_final.pt")
        **plot_training_history (bool):
            Plots training and test curves vs epochs at the end of training
        **kwargs:
            One can also pass kwargs for utils.datatransform class
            to perform the augmentation "on-the-fly" (e.g. rotation=True,
            gauss=[20, 60], ...)

    Example:

    >>> # Load 4 numpy arrays with training and test data
    >>> dataset = np.load('training_data.npz')
    >>> images_all = dataset['X_train']
    >>> labels_all = dataset['y_train']
    >>> images_test_all = dataset['X_test']
    >>> labels_test_all = dataset['y_test']
    >>> # Train a model
    >>> netr = atomnet.trainer(
    >>>     images_all, labels_all,
    >>>     images_test_all, labels_test_all,
    >>>     training_cycles=500)
    >>> trained_model = netr.run()
    """
    def __init__(self,
                 X_train: training_data_types,
                 y_train: training_data_types,
                 X_test: training_data_types,
                 y_test: training_data_types,
                 training_cycles: int,
                 model: str = 'dilUnet',
                 IoU: bool = False,
                 seed: int = 1,
                 batch_seed: int = None,
                 **kwargs: Union[int, List, str, bool]) -> None:
        """
        Initialize single model trainer
        """
        if seed:
            set_train_rng(seed)
        if batch_seed is None:
            np.random.seed(seed)
        else:
            np.random.seed(batch_seed)

        self.batch_size = kwargs.get("batch_size", 32)
        (self.X_train, self.y_train,
         self.X_test, self.y_test,
         self.num_classes) = preprocess_training_data(
                                X_train, y_train, X_test, y_test,
                                self.batch_size)
        use_batchnorm = kwargs.get('use_batchnorm', True)
        use_dropouts = kwargs.get('use_dropouts', False)
        upsampling = kwargs.get('upsampling', "bilinear")
        if not isinstance(model, str) and hasattr(model, "state_dict"):
            self.net = model
        elif isinstance(model, str) and model == 'dilUnet':
            with_dilation = kwargs.get('with_dilation', True)
            nb_filters = kwargs.get('nb_filters', 16)
            layers = kwargs.get("layers", [1, 2, 2, 3])
            self.net = dilUnet(
                self.num_classes, nb_filters, use_dropouts,
                use_batchnorm, upsampling, with_dilation,
                layers=layers
            )
        elif isinstance(model, str) and model == 'dilnet':
            nb_filters = kwargs.get('nb_filters', 25)
            layers = kwargs.get("layers", [1, 3, 3, 3])
            self.net = dilnet(
                self.num_classes, nb_filters,
                use_dropouts, use_batchnorm, upsampling,
                layers=layers
            )
        else:
            raise NotImplementedError(
                "Currently implemented models are 'dilUnet' and 'dilnet'"
            )
        if torch.cuda.is_available():
            self.net.cuda()
        else:
            warnings.warn(
                "No GPU found. The training can be EXTREMELY slow",
                UserWarning
            )
        loss = kwargs.get('loss', "ce")
        if loss == 'dice':
            self.criterion = losses_metrics.dice_loss()
        elif loss == 'focal':
            self.criterion = losses_metrics.focal_loss()
        elif loss == 'ce' and self.num_classes == 1:
            self.criterion = torch.nn.BCEWithLogitsLoss()
        elif loss == 'ce' and self.num_classes > 2:
            self.criterion = torch.nn.CrossEntropyLoss()
        else:
            raise NotImplementedError(
                "Select Dice loss ('dice'), focal loss ('focal') or"
                " cross-entropy loss ('ce')"
            )
        self.batch_idx_train = np.random.randint(
            0, len(self.X_train), training_cycles)
        self.batch_idx_test = np.random.randint(
            0, len(self.X_test), training_cycles)
        auglist = ["custom_transform", "zoom", "gauss_noise", "jitter",
                   "poisson_noise", "contrast", "salt_and_pepper", "blur",
                   "resize", "rotation","background"]
        self.augdict = {k: kwargs[k] for k in auglist if k in kwargs.keys()}
        self.optimizer = torch.optim.Adam(self.net.parameters(), lr=1e-3)
        self.training_cycles = training_cycles
        self.iou = IoU
        self.swag = kwargs.get("swag", False)
        if self.swag:
            self.recent_weights = {}
        self.print_loss = kwargs.get("print_loss", 100)
        self.savedir = kwargs.get("savedir", "./")
        self.savename = kwargs.get("savename", "model")
        self.plot_training_history = kwargs.get("plot_training_history", True)
        self.train_loss, self.test_loss = [], []
        if isinstance(model, str):
            self.meta_state_dict = {
                'model_type': model,
                'batchnorm': use_batchnorm,
                'dropout': use_dropouts,
                'upsampling': upsampling,
                'nb_filters': nb_filters,
                'layers': layers,
                'nb_classes': self.num_classes,
                'weights': self.net.state_dict()
            }
            if "with_dilation" in locals():
                self.meta_state_dict["with_dilation"] = with_dilation
        else:
            self.meta_state_dict = {
                'nb_classes': self.num_classes,
                'weights': self.net.state_dict()
            }

    def dataloader(self, batch_num: int, mode: str = 'train') -> Tuple[torch.Tensor]:
        """
        Generates 2 batches of 4D tensors (images and masks)
        """
        # Generate batch of training images with corresponding ground truth
        if mode == 'test':
            images = self.X_test[batch_num][:self.batch_size]
            labels = self.y_test[batch_num][:self.batch_size]
        else:
            images = self.X_train[batch_num][:self.batch_size]
            labels = self.y_train[batch_num][:self.batch_size]
        # "Augment" data if applicable
        if len(self.augdict) > 0:
            dt = datatransform(
                self.num_classes, "channel_first", 'channel_first',
                True, len(self.train_loss), **self.augdict)
            images, labels = dt.run(
                images[:, 0, ...], unsqueeze_channels(labels, self.num_classes))
        # Transform images and ground truth to torch tensors and move to GPU
        images = torch.from_numpy(images).float()
        if self.num_classes == 1:
            labels = torch.from_numpy(labels).float()
        else:
            labels = torch.from_numpy(labels).long()
        if torch.cuda.is_available():
            images, labels = images.cuda(), labels.cuda()
        return images, labels

    def train_step(self, img: torch.Tensor, lbl: torch.Tensor) -> Tuple[float]:
        """
        Propagates image(s) through a network to get model's prediction
        and compares predicted value with ground truth; then performs
        backpropagation to compute gradients and optimizes weights.
        """
        self.net.train()
        self.optimizer.zero_grad()
        prob = self.net(img)
        loss = self.criterion(prob, lbl)
        loss.backward()
        self.optimizer.step()
        if self.iou:
            iou_score = losses_metrics.IoU(
                lbl, prob, self.num_classes).evaluate()
            return (loss.item(), iou_score)
        return (loss.item(),)

    def test_step(self, img: torch.Tensor, lbl: torch.Tensor) -> float:
        """
        Forward pass for test data with deactivated autograd engine
        """
        self.net.eval()
        with torch.no_grad():
            prob = self.net(img)
            loss = self.criterion(prob, lbl)
        if self.iou:
            iou_score = losses_metrics.IoU(
                lbl, prob, self.num_classes).evaluate()
            return (loss.item(), iou_score)
        return (loss.item(),)

    def run(self) -> Type[torch.nn.Module]:
        """
        Trains a neural network for *N* epochs by passing a single pair of
        training images and labels and a single pair of test images
        and labels at each epoch. Saves the final model weights.
        """
        for e in range(self.training_cycles):
            # Get training images/labels
            images, labels = self.dataloader(
                self.batch_idx_train[e], mode='train')
            # Training step
            loss = self.train_step(images, labels)
            self.train_loss.append(loss[0])
            images_, labels_ = self.dataloader(
                self.batch_idx_test[e], mode='test')
            # Test step
            loss_ = self.test_step(images_, labels_)
            self.test_loss.append(loss_[0])

            if self.swag and self.training_cycles - e <= 30:
                i_ = 30 - (self.training_cycles - e)
                state_dict_ = OrderedDict()
                for k, v in self.net.state_dict().items():
                    state_dict_[k] = copy.deepcopy(v).cpu()
                self.recent_weights[i_] = state_dict_

            if e == 0 or (e+1) % self.print_loss == 0:
                if torch.cuda.is_available():
                    gpu_usage = gpu_usage_map(torch.cuda.current_device())
                else:
                    gpu_usage = ['N/A ', ' N/A']
                if self.iou:
                    print('Epoch {} ...'.format(e+1),
                          'Training loss: {} ...'.format(
                              np.around(self.train_loss[-1], 4)),
                          'Test loss: {} ...'.format(
                              np.around(self.test_loss[-1], 4)),
                          'Train IoU: {} ...'.format(
                              np.around(loss[1], 4)),
                          'Test IoU: {} ...'.format(
                              np.around(loss_[1], 4)),
                          'GPU memory usage: {}/{}'.format(
                              gpu_usage[0], gpu_usage[1]))
                else:
                    print('Epoch {} ...'.format(e+1),
                          'Training loss: {} ...'.format(
                              np.around(self.train_loss[-1], 4)),
                          'Test loss: {} ...'.format(
                              np.around(self.test_loss[-1], 4)),
                          'GPU memory usage: {}/{}'.format(
                              gpu_usage[0], gpu_usage[1]))
        # Save final model weights
        torch.save(self.meta_state_dict,
                   os.path.join(self.savedir,
                   self.savename+'_metadict_final_weights.tar'))
        # Run evaluation (by passing all the test data) for a final model state
        running_loss_test, running_iou_test = 0, 0
        for idx in range(len(self.X_test)):
            images_, labels_ = self.dataloader(idx, mode='test')
            loss_ = self.test_step(images_, labels_)
            running_loss_test += loss_[0]
            if self.iou:
                running_iou_test += loss_[1]
        print('Model (final state) evaluation loss:',
              np.around(running_loss_test / len(self.X_test), 4))
        if self.iou:
            print('Model (final state) IoU:',
                  np.around(running_iou_test / len(self.X_test), 4))
        if self.plot_training_history:
            plot_losses(self.train_loss, self.test_loss)
        return self.net


class ensemble_trainer:
    """
    Trains multiple deep learning models, each with its own unique trajectory

    Args:
        X_train (numpy array): Training images
        y_train (numpy array): Training labels (aka ground truth aka masks)
        X_test (numpy array): Test images
        y_test (numpy array): Test labels
        n_models (int): number of models in ensemble
        model(str): 'dilUnet' or 'dilnet'. See atomai.models for details
        strategy (str): Select between 'from_scratch', 'from_baseline' and 'swag'.
            If 'from_scratch' is selected, it trains *n* models independently
            starting each time with different random initialization. If
            'from_baseline' is selected, it trains one basemodel for *N* epochs
            and then uses it as a baseline to train multiple ensemble models
            for n epochs (*n* << *N*), each with different random shuffling of batches.
            If 'swag' is selected, it performs SWAG-like sampling of weights at the
            end of a single model training. If 'multiswag' is selected, it combines
            'from_baseline' and 'swag' methods, i.e. it performes weights sampling
            at the end of each independent training.
        training_cycles_base (int): Number of training iterations for baseline model
        training_cycles_ensemble (int): Number of training iterations for every ensemble model
        filename (str): Filepath for saving weights
        **kwargs:
            One can also pass kwargs to atomai.atomnet.trainer class for adjusting
            network parameters (e.g. batchnorm=True, nb_filters=25, etc.)
            and to atomai.utils.datatransform class to perform the augmentation
            "on-the-fly" (e.g. rotation=True, gauss=[20, 60], etc.)
    """
    def __init__(self, X_train: np.ndarray, y_train: np.ndarray,
                 X_test: np.ndarray = None, y_test: np.ndarray = None,
                 n_models=30, model: str = "dilUnet",
                 strategy: str = "from_baseline",
                 training_cycles_base: int = 1000,
                 training_cycles_ensemble: int = 50,
                 filename: str = "./model", **kwargs: Dict) -> None:
        """
        Initializes parameters of ensemble trainer
        """
        if X_test is None or y_test is None:
            X_train, X_test, y_train, y_test = train_test_split(
                X_train, y_train, test_size=kwargs.get("test_size", 0.15),
                shuffle=True, random_state=0)
        set_train_rng(seed=1)
        self.X_train, self.y_train = X_train, y_train
        self.X_test, self.y_test = X_test, y_test
        self.model_type, self.n_models = model, n_models
        self.strategy = strategy
        if self.strategy not in ["from_baseline", "from_scratch",
                                 "swag", "multiswag"]:
            raise NotImplementedError(
                "Select 'from_baseline' 'from_scratch', 'swag' or 'multiswag' strategy")
        self.iter_base = training_cycles_base
        if self.strategy == "from_baseline":
            self.iter_ensemble = training_cycles_ensemble
        self.filename, self.kdict = filename, kwargs
        if self.strategy == "swag" or self.strategy == "multiswag":
            self.kdict["swag"] = True
            self.kdict["use_batchnorm"] = False
        self.ensemble_state_dict = {}

    def train_baseline(self,
                       seed: int = 1,
                       batch_seed: int = 1) -> Type[trainer]:
        """
        Trains a single "baseline" model
        """
        if self.strategy == "from_baseline":
            print('Training baseline model:')
        trainer_base = trainer(
            self.X_train, self.y_train,
            self.X_test, self.y_test,
            self.iter_base, self.model_type,
            seed=seed, batch_seed=batch_seed,
            plot_training_history=True,
            savename=self.filename + "_base",
            **self.kdict)
        _ = trainer_base.run()

        return trainer_base

    def train_ensemble_from_baseline(self,
                                     basemodel: Union[OrderedDict, Type[torch.nn.Module]],
                                     **kwargs: Dict) -> ensemble_out:
        """
        Trains ensemble of models starting each time from baseline weights

        Args:
            basemodel (pytorch object): Baseline model or baseline weights
            **kwargs: Updates kwargs from the ensemble class initialization
                (can be useful for iterative training)
        """
        if len(kwargs) != 0:
            for k, v in kwargs.items():
                self.kdict[k] = v
        if isinstance(basemodel, OrderedDict):
            initial_model_state_dict = copy.deepcopy(basemodel)
        else:
            initial_model_state_dict = copy.deepcopy(basemodel.state_dict())
        n_models = kwargs.get("n_models")
        if n_models is not None:
            self.n_models = n_models
        filename = kwargs.get("filename")
        training_cycles_ensemble = kwargs.get("training_cycles_ensemble")
        if training_cycles_ensemble is not None:
            self.iter_ensemble = training_cycles_ensemble
        if filename is not None:
            self.filename = filename
        print('Training ensemble models:')
        for i in range(self.n_models):
            print('Ensemble model', i+1)
            trainer_i = trainer(
                self.X_train, self.y_train, self.X_test, self.y_test,
                self.iter_ensemble, self.model_type, batch_seed=i+1,
                print_loss=10, plot_training_history=False, **self.kdict)
            self.update_weights(trainer_i.net.state_dict().values(),
                                initial_model_state_dict.values())
            trained_model_i = trainer_i.run()
            self.ensemble_state_dict[i] = trained_model_i.state_dict()
            self.save_ensemble_metadict(trainer_i.meta_state_dict)
        return self.ensemble_state_dict, trainer_i.net

    def train_ensemble_from_scratch(self) -> ensemble_out:
        """
        Trains ensemble of models starting every time from scratch with
        different initialization (for both weights and batches shuffling)
        """
        print("Training ensemble models:")
        for i in range(self.n_models):
            print("Ensemble model {}".format(i + 1))
            trainer_i = self.train_baseline(seed=i+1, batch_seed=i+1)
            self.ensemble_state_dict[i] = trainer_i.net.state_dict()
            self.save_ensemble_metadict(trainer_i.meta_state_dict)
        return self.ensemble_state_dict, trainer_i.net

    def train_swag(self) -> ensemble_out:
        """
        Performs SWAG-like weights sampling at the end of single model training
        """
        trainer_i = self.train_baseline()
        sampled_weights = sample_weights(
            trainer_i.recent_weights, self.n_models)
        self.ensemble_state_dict = sampled_weights
        return self.ensemble_state_dict, trainer_i.net

    def train_multiswag(self) -> ensemble_out:
        """
        Trains ensemble of models starting every time from scratch with
        different initialization (for both weights and batches shuffling)
        with a SWAG-like weights sampling at the end of each model training
        """
        print("Training ensemble models:")
        for i in range(self.n_models):
            print("Ensemble model {}".format(i + 1))
            trainer_i = self.train_baseline(seed=i, batch_seed=i)
            sampled_weights = sample_weights(
                trainer_i.recent_weights, 30)
            for k, v in sampled_weights.items():
                self.ensemble_state_dict[(30 * i) + k] = copy.deepcopy(v)
        return self.ensemble_state_dict, trainer_i.net

    def save_ensemble_metadict(self, meta_state_dict: Dict) -> None:
        """
        Saves meta dictionary with ensemble weights and key information about
        model's structure (needed to load it back) to disk'
        """
        ensemble_metadict = copy.deepcopy(meta_state_dict)
        ensemble_metadict["weights"] = self.ensemble_state_dict
        torch.save(ensemble_metadict, self.filename + "_ensemble.tar")

    @classmethod
    def update_weights(cls,
                       statedict1: Dict[str, torch.Tensor],
                       statedict2: Dict[str, torch.Tensor]) -> None:
        """
        Updates (in place) state dictionary of pytorch model
        with weights from another model with the same structure;
        skips layers that have different dimensions
        (e.g. if one model is for single class classification
        and the other one is for multiclass classification,
        then the last layer wights are not updated)
        """
        for p1, p2 in zip(statedict1, statedict2):
            if p1.shape == p2.shape:
                p1.copy_(p2)

    def set_data(self,
                 X_train: np.ndarray, y_train: np.ndarray,
                 X_test: np.ndarray = None, y_test: np.ndarray = None) -> None:
        """
        Sets data for ensemble training (useful for iterative training)
        """
        if X_test is None or y_test is None:
            X_train, X_test, y_train, y_test = train_test_split(
                X_train, y_train, test_size=self.kdict.get("test_size", 0.15),
                shuffle=True, random_state=0)
        self.X_train = X_train
        self.y_train = y_train
        self.X_test = X_test
        self.y_test = y_test

    def run(self) -> ensemble_out:
        """
        Trains a baseline model and ensemble of models
        """
        if self.strategy == 'from_baseline':
            base_trainer = self.train_baseline()
            ensemble, smodel = self.train_ensemble_from_baseline(base_trainer.net)
        elif self.strategy == 'from_scratch':
            ensemble, smodel = self.train_ensemble_from_scratch()
        elif self.strategy == 'swag':
            ensemble, smodel = self.train_swag()
        elif self.strategy == 'multiswag':
            ensemble, smodel = self.train_multiswag()
        else:
            raise NotImplementedError(
                "The strategy must be 'from_baseline', 'from_scratch', 'swag' or 'multiswag'")
        return ensemble, smodel


def train_single_model(images_all: training_data_types,
                       labels_all: training_data_types,
                       images_test_all: training_data_types,
                       labels_test_all: training_data_types,
                       training_cycles: int,
                       model: Union[str, Callable] = 'dilUnet',
                       IoU: bool = False,
                       seed: int = 1,
                       batch_seed: int = None,
                       **kwargs: Union[int, List, str, bool]
                       ) -> Type[torch.nn.Module]:
    """
    "Wrapper function" for class atomai.atomnet.trainer
    """
    t = trainer(images_all, labels_all, images_test_all, labels_test_all,
                training_cycles, model, IoU, seed, batch_seed, **kwargs)
    trained_model = t.run()
    return trained_model


def train_swag_model(images_all: training_data_types,
                     labels_all: training_data_types,
                     images_test_all: training_data_types,
                     labels_test_all: training_data_types,
                     training_cycles: int,
                     model: Union[str, Callable] = 'dilUnet',
                     IoU: bool = False,
                     seed: int = 1,
                     batch_seed: int = None,
                     **kwargs: Union[int, List, str, bool]
                     ) -> Type[torch.nn.Module]:
    """
    "Wrapper function" for class atomai.atomnet.trainer
    with SWAG-like weights sampling at the end of model training
    """
    kwargs["swag"] = True
    kwargs["use_batchnorm"] = False
    t = trainer(images_all, labels_all, images_test_all, labels_test_all,
                training_cycles, model, IoU, seed, batch_seed, **kwargs)
    trained_model = t.run()
    sampled_weights = sample_weights(t.recent_weights)

    filename = kwargs.get("savename", "model")
    swag_metadict = copy.deepcopy(t.meta_state_dict)
    swag_metadict["weights"] = sampled_weights
    torch.save(swag_metadict, filename + "_swag.tar")

    return sampled_weights, trained_model
