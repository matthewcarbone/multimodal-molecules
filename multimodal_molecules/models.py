from copy import deepcopy
from itertools import combinations
from functools import cached_property, cache
import json
from pathlib import Path
import pickle
from time import perf_counter
from tqdm import tqdm
from warnings import warn

from monty.json import MSONable
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.inspection import permutation_importance
from sklearn.metrics import accuracy_score, balanced_accuracy_score
from sklearn.model_selection import train_test_split

from multimodal_molecules.data import get_dataset


class Timer:
    def __enter__(self):
        self._time = perf_counter()
        return self

    def __exit__(self, type, value, traceback):
        self._time = perf_counter() - self._time

    @property
    def dt(self):
        return self._time


def get_all_combinations(n):
    L = [ii for ii in range(n)]
    combos = []
    for nn in range(len(L)):
        combos.extend(list(combinations(L, nn + 1)))
    return combos


def predict_rf(rf, X):
    return np.array([est.predict(X) for est in rf.estimators_]).T


def get_split(
    data,
    functional_group="Alcohol",
    which_XANES=["C-XANES"],
    min_fg_occurrence=0.02,
    max_fg_occurrence=0.98,
    test_size=0.1,
    random_state=42,
):
    """Summary

    Parameters
    ----------
    data : dict
        Data as produced by multimodal_molecules.data:get_dataset.
    functional_group : str, optional
        The functional group to use
    which_XANES : list, optional
        Description
    min_fg_occurrence : float, optional
        Description
    max_fg_occurrence : float, optional
        Description
    test_size : float, optional
        Description
    random_state : int, optional
        Description

    Returns
    -------
    dict
    """

    loc = {key: value for key, value in locals() if key != "data"}

    X = np.concatenate([data[xx] for xx in which_XANES], axis=1)
    y = data["FG"][functional_group]

    # Check that the occurence of the functional groups falls into the
    # specified sweet spot
    p_total = y.sum() / len(y)
    if not min_fg_occurrence < p_total < max_fg_occurrence:
        warn(f"p_total=={p_total:.02f} too small/large")
        return None

    # Get the split. Note that the training split here includes validation.
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.10, random_state=random_state
    )

    return {
        "locals": loc,
        "X_train": X_train,
        "X_test": X_test,
        "y_train": y_train,
        "y_test": y_test,
    }


class Results(MSONable):
    """A full report for all functional groups given a set of conditions."""

    @classmethod
    def from_file(cls, path):
        with open(path, "r") as f:
            d = json.loads(json.load(f))
        klass = cls.from_dict(d)
        klass._data_loaded_from = str(Path(path).parent)
        return klass

    @property
    def report(self):
        return self._report

    @cached_property
    def models(self):
        """Returns a dictionary of the loaded models. Note this requires that
        _data_loaded_from is set. This only happens when loading the class from
        a json file.

        Returns
        -------
        dict
        """

        if self._data_loaded_from is None:
            warn(
                "Use this after loading from json and saving the pickled "
                "models. Returning None"
            )
            return None

        base_name = self._conditions.replace(",", "_")
        path = Path(self._data_loaded_from) / f"{base_name}_models.pkl"
        return pickle.load(open(path, "rb"))

        d = dict()
        for fname in Path(self._data_loaded_from).glob("*.pkl"):
            key = str(fname).split(".pkl")[0].split("_")[1]
            d[key] = pickle.load(open(fname, "rb"))
        return d

    @cached_property
    def train_test_indexes(self):
        if self._data_size is None:
            raise RuntimeError("Run experiments first to calculate data size")
        np.random.seed(self._random_state)
        N = self._data_size
        size = int(self._test_size * N)
        test_indexes = np.random.choice(N, size=size, replace=False).tolist()
        assert len(test_indexes) == len(np.unique(test_indexes))
        train_indexes = list(set(np.arange(N).tolist()) - set(test_indexes))
        assert set(test_indexes).isdisjoint(set(train_indexes))
        return sorted(train_indexes), sorted(test_indexes)

    @cache
    def get_data(self, input_data_directory):
        xanes_path = Path(input_data_directory) / self._xanes_data_name
        index_path = Path(input_data_directory) / self._index_data_name
        return get_dataset(xanes_path, index_path, self._conditions)

    def _get_xanes_data(self, data):
        """Select the keys that contain the substring "XANES". Also returns
        the lenght of the keys available."""

        xanes_keys_avail = [
            cc for cc in self._conditions.split(",") if "XANES" in cc
        ]
        o1 = self._offset_left
        o2 = self._offset_right
        return np.concatenate(
            [data[key][:, o1:o2] for key in xanes_keys_avail],
            axis=1,
        ), len(xanes_keys_avail)

    def get_train_test_split(self, data, xanes="C,N,O"):
        """Gets the training and testing splits from provided data.

        Parameters
        ----------
        data : dict
            The data as loaded by the ``get_dataset`` function.
        xanes : str, optional
            Description

        Returns
        -------
        dict
            A dictionary containing the trainind/testing data for this set of
            results.
        """

        xanes = [f"{xx}-XANES" for xx in xanes.split(",")]
        conditions = self._conditions.split(",")
        assert set(xanes).issubset(set(conditions))
        indexes = [conditions.index(xx) for xx in xanes]

        train_idx, test_idx = self.train_test_indexes

        xanes_data, n_xanes_types = self._get_xanes_data(data)
        ssl = xanes_data.shape[1] // n_xanes_types  # Single spectrum length

        current_xanes_data = np.concatenate(
            [
                xanes_data[:, ssl * ii : ssl * (ii + 1)] for ii in indexes
            ],  # noqa
            axis=1,
        )

        xanes_data_train = current_xanes_data[train_idx, :]
        xanes_data_test = current_xanes_data[test_idx, :]

        functional_groups = data["FG"]
        keys = functional_groups.keys()
        train_fg = {key: functional_groups[key][train_idx] for key in keys}
        test_fg = {key: functional_groups[key][test_idx] for key in keys}

        return {
            "x_train": xanes_data_train,
            "x_test": xanes_data_test,
            "y_train": train_fg,
            "y_test": test_fg,
            "unique_functional_groups": list(functional_groups),
        }

    def __init__(
        self,
        conditions,
        xanes_data_name="221205_xanes.pkl",
        index_data_name="221205_index.csv",
        offset_left=None,
        offset_right=None,
        test_size=0.6,
        random_state=42,
        min_fg_occurrence=0.02,
        max_fg_occurrence=0.98,
        data_size=None,
        data_loaded_from=None,
        report=None,
    ):
        self._conditions = ",".join(sorted(conditions.split(",")))
        self._xanes_data_name = xanes_data_name
        self._index_data_name = index_data_name
        self._offset_left = offset_left
        self._offset_right = offset_right
        self._test_size = test_size
        self._random_state = random_state
        self._min_fg_occurrence = min_fg_occurrence
        self._max_fg_occurrence = max_fg_occurrence
        self._data_size = data_size
        self._data_loaded_from = data_loaded_from
        if report is None:
            self._report = {}
        else:
            self._report = report

    def run_experiments(
        self,
        input_data_directory="data/221205",
        output_data_directory=None,
        n_jobs=2,
        debug=-1,
        compute_feature_importance=True,
    ):
        """Runs all experiments corresponding to the functional groups
        and the initially provided conditions.

        Parameters
        ----------
        input_data_directory : str
            The location of the input data. Should contain the xanes.pkl-like
            file and the index.csv-like file. The specific names of these files
            are provided at class instantiation.
        output_data_directory : os.PathLike, optional
            The location of the target directory for saving results. If None,
            no results are saved to disk and must be done manually.
        n_jobs : int, optional
            The number of jobs/parallel processes to feed to the RandomForest
            model and the feature impotance ranking functions.
        debug : bool, optional
            If >0, iterates only through that many calculations.
        compute_feature_importance : bool, optional
            Computes the feature importances using the permutation method.
            Note that this is quite expensive. Likely to take aroudn 2 minutes
            or so per model even at full parallelization.
        """

        print("\n")
        print("--------------------------------------------------------------")
        print("\n")

        data = self.get_data(input_data_directory)
        xanes_data, n_xanes_types = self._get_xanes_data(data)
        self._data_size = xanes_data.shape[0]
        ssl = xanes_data.shape[1] // n_xanes_types  # Single spectrum length
        train_indexes, test_indexes = self.train_test_indexes

        base_name = self._conditions.replace(",", "_")

        print(f"Total XANES data has shape {xanes_data.shape}")
        L = len(data["FG"])
        print(f"Total of {L} functional groups\n")

        xanes_index_combinations = get_all_combinations(n_xanes_types)

        conditions_list = base_name.split("_")
        models = dict()
        for combo in xanes_index_combinations:
            current_conditions_name = "_".join(
                [conditions_list[jj] for jj in combo]
            )
            current_xanes_data = np.concatenate(
                [
                    xanes_data[:, ssl * ii : ssl * (ii + 1)] for ii in combo
                ],  # noqa
                axis=1,
            )
            print(
                f"Current XANES combo={combo} name={current_conditions_name} "
                f"shape={current_xanes_data.shape}"
            )

            for ii, (fg_name, binary_targets) in enumerate(data["FG"].items()):
                ename = f"{current_conditions_name}-{fg_name}"

                # Check that the occurence of the functional groups falls into
                # the specified sweet spot
                p_total = binary_targets.sum() / len(binary_targets)
                if not (
                    self._min_fg_occurrence < p_total < self._max_fg_occurrence
                ):
                    print(
                        f"\t[{(ii+1):03}/{L:03}] {ename} occurence "
                        f"{p_total:.04f} - continuing",
                        flush=True,
                    )
                    continue

                x_train = current_xanes_data[train_indexes, :]
                x_test = current_xanes_data[test_indexes, :]
                y_train = binary_targets[train_indexes]
                y_test = binary_targets[test_indexes]

                p_test = y_test.sum() / len(y_test)
                p_train = y_train.sum() / len(y_train)

                print(
                    f"\t[{(ii+1):03}/{L:03}] {ename} "
                    f"occ. total={p_total:.04f} | train={p_train:.04f} | "
                    f"test={p_test:.04f} ",
                    end="",
                )

                with Timer() as timer:
                    # Train the model
                    model = RandomForestClassifier(
                        n_jobs=n_jobs, random_state=self._random_state
                    )
                    model.fit(x_train, y_train)

                    if output_data_directory is not None:
                        models[ename] = deepcopy(model)

                print(f"- training: {timer.dt:.01f} s ", end="")

                with Timer() as timer:
                    y_test_pred = model.predict(x_test)
                    y_train_pred = model.predict(x_train)

                    # Accuracies and other stuff
                    self._report[ename] = {
                        "p_total": p_total,
                        "p_train": p_train,
                        "p_test": p_test,
                        "test_accuracy": accuracy_score(y_test, y_test_pred),
                        "train_accuracy": accuracy_score(
                            y_train, y_train_pred
                        ),
                        "test_balanced_accuracy": balanced_accuracy_score(
                            y_test, y_test_pred
                        ),
                        "train_balanced_accuracy": balanced_accuracy_score(
                            y_train, y_train_pred
                        ),
                    }

                    if compute_feature_importance:
                        # Standard feature importance from the RF model
                        # This is very fast
                        f_importance = np.array(
                            [
                                tree.feature_importances_
                                for tree in model.estimators_
                            ]
                        )

                        # Append to the report
                        self._report[ename]["feature_importance"] = {
                            "importances_mean": f_importance.mean(axis=0),
                            "importances_std": f_importance.std(axis=0),
                        }

                        # More accurate permutation feature importance
                        p_importance = permutation_importance(
                            model, x_test, y_test, n_jobs=n_jobs
                        )
                        p_importance.pop("importances")

                        # Append to the report
                        self._report[ename][
                            "permutation_feature_importance"
                        ] = {
                            "importances_mean": p_importance.mean(axis=0),
                            "importances_std": p_importance.std(axis=0),
                        }

                print(f"- report/save: {timer.dt:.01f} s")

                if debug > 0:
                    if ii >= debug:
                        print("\tIn testing mode- ending early!", flush=True)
                        break

        if output_data_directory is not None:
            Path(output_data_directory).mkdir(exist_ok=True, parents=True)
            root = Path(output_data_directory)
            report_path = root / f"{base_name}.json"
            with open(report_path, "w") as f:
                json.dump(self.to_json(), f, indent=4)
            print(f"\nReport saved to {report_path}")

            # Save the model itself
            model_path = root / f"{base_name}_models.pkl"
            pickle.dump(
                models,
                open(model_path, "wb"),
                protocol=pickle.HIGHEST_PROTOCOL,
            )
            print(f"Report saved to {model_path}")


def validate(path, data_directory):
    """A helper function for validating that the results of the models
    (pickled) are the same as those which were stored in the reports.

    Parameters
    ----------
    path : os.PathLike
        Path to the json file which contains the model results.
    input_data_directory : TYPE
        Description
    """

    results = Results.from_file(path)
    xanes_data_path = Path(data_directory) / results._xanes_data_name
    index_data_path = Path(data_directory) / results._index_data_name
    conditions = results._conditions
    data = get_dataset(xanes_data_path, index_data_path, conditions)
    _, test_idx = results.train_test_indexes
    models = results.models

    for key, model in tqdm(models.items()):
        # Get the XANES keys
        xk = [xx for xx in key.split("XANES")[:-1]]
        xk = [xx.replace("-", "").replace("_", "") for xx in xk]
        xk = [f"{xx}-XANES" for xx in xk]

        # Get the input data
        o1 = results._offset_left
        o2 = results._offset_right
        X = np.concatenate(
            [data[key][:, o1:o2] for key in xk],
            axis=1,
        )

        # Get the predictions and the ground truth for the model
        preds = model.predict(X[test_idx, :])
        fg = key.split("XANES-")[-1]
        targets = data["FG"][fg][test_idx]
        balanced_acc = balanced_accuracy_score(targets, preds)

        # Get the previously cached results for the accuracy
        previous_balanced_acc = results.report[key]["test_balanced_accuracy"]

        # print(f"{previous_balanced_acc:.02f} | {balanced_acc:.02f}")
        assert np.allclose(balanced_acc, previous_balanced_acc)
