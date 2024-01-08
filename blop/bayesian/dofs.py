import time as ttime
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Tuple, Union

import numpy as np
import pandas as pd
from ophyd import Signal, SignalRO

DEFAULT_BOUNDS = (-5.0, +5.0)
DOF_FIELDS = ["description", "readback", "lower_limit", "upper_limit", "units", "active", "read_only", "tags"]

numeric = Union[float, int]


class ReadOnlyError(Exception):
    ...


def _validate_dofs(dofs):
    dof_names = [dof.name for dof in dofs]

    # check that dof names are unique
    unique_dof_names, counts = np.unique(dof_names, return_counts=True)
    duplicate_dof_names = unique_dof_names[counts > 1]
    if len(duplicate_dof_names) > 0:
        raise ValueError(f"Duplicate name(s) in supplied dofs: {duplicate_dof_names}")

    return list(dofs)


@dataclass
class DOF:
    """A degree of freedom (DOF), to be used by an agent.

    Parameters
    ----------
    name: str
        The name of the DOF. This is used as a key.
    description: str
        A longer name for the DOF.
    device: Signal, optional
        An ophyd device. If None, a dummy ophyd device is generated.
    limits: tuple, optional
        A tuple of the lower and upper limit of the DOF. If the DOF is not read-only, the agent
        will not explore outside the limits. If the DOF is read-only, the agent will reject all
        sampled data where the DOF is outside the limits.
    read_only: bool
        If True, the agent will not try to set the DOF. Must be set to True if the supplied ophyd
        device is read-only.
    active: bool
        If True, the agent will try to use the DOF in its optimization. If False, the agent will
        still read the DOF but not include it any model or acquisition function.
    units: str
        The units of the DOF (e.g. mm or deg). This is only for plotting and general housekeeping.
    tags: list
        A list of tags. These make it easier to subset large groups of dofs.
    latent_group: optional
        An agent will fit latent dimensions to all DOFs with the same latent_group. If None, the
        DOF will be modeled independently.
    """

    device: Signal = None
    description: str = None
    name: str = None
    limits: Tuple[float, float] = (-10.0, 10.0)
    units: str = ""
    read_only: bool = False
    active: bool = True
    tags: list = field(default_factory=list)
    log: bool = False
    latent_group: str = None

    # Some post-processing. This is specific to dataclasses
    def __post_init__(self):
        self.uuid = str(uuid.uuid4())

        if self.name is None:
            self.name = self.device.name if hasattr(self.device, "name") else self.uuid

        if self.device is None:
            self.device = Signal(name=self.name)

        if not self.read_only:
            # check that the device has a put method
            if isinstance(self.device, SignalRO):
                raise ValueError("Must specify read_only=True for a read-only device!")

        if self.latent_group is None:
            self.latent_group = str(uuid.uuid4())

        # all dof degrees of freedom are hinted
        self.device.kind = "hinted"

    @property
    def lower_limit(self):
        return float(self.limits[0])

    @property
    def upper_limit(self):
        return float(self.limits[1])

    @property
    def readback(self):
        return self.device.read()[self.device.name]["value"]

    @property
    def summary(self) -> pd.Series:
        series = pd.Series(index=DOF_FIELDS)
        for attr in series.index:
            series[attr] = getattr(self, attr)
        return series

    @property
    def label(self) -> str:
        return f"{self.name}{f' [{self.units}]' if len(self.units) > 0 else ''}"

    @property
    def has_model(self):
        return hasattr(self, "model")


class DOFList(Sequence):
    def __init__(self, dofs: list = []):
        _validate_dofs(dofs)
        self.dofs = dofs

    def __getitem__(self, i):
        if type(i) is int:
            return self.dofs[i]
        elif type(i) is str:
            return self.dofs[self.names.index(i)]
        else:
            raise ValueError(f"Invalid index {i}. A DOFList must be indexed by either an integer or a string.")

    def __len__(self):
        return len(self.dofs)

    def __repr__(self):
        return self.summary.__repr__()

    # def _repr_html_(self):
    #     return self.summary._repr_html_()

    @property
    def summary(self) -> pd.DataFrame:
        table = pd.DataFrame(columns=DOF_FIELDS)
        for dof in self.dofs:
            for attr in table.columns:
                table.loc[dof.name, attr] = getattr(dof, attr)

        # convert dtypes
        for attr in ["readback", "lower_limit", "upper_limit"]:
            table[attr] = table[attr].astype(float)

        for attr in ["read_only", "active"]:
            table[attr] = table[attr].astype(bool)

        return table

    @property
    def names(self) -> list:
        return [dof.name for dof in self.dofs]

    @property
    def devices(self) -> list:
        return [dof.device for dof in self.dofs]

    @property
    def device_names(self) -> list:
        return [dof.device.name for dof in self.dofs]

    @property
    def lower_limits(self) -> np.array:
        return np.array([dof.lower_limit for dof in self.dofs])

    @property
    def upper_limits(self) -> np.array:
        return np.array([dof.upper_limit for dof in self.dofs])

    @property
    def limits(self) -> np.array:
        """
        Returns a (n_dof, 2) array of bounds.
        """
        return np.c_[self.lower_limits, self.upper_limits]

    @property
    def readback(self) -> np.array:
        return np.array([dof.readback for dof in self.dofs])

    def add(self, dof):
        _validate_dofs([*self.dofs, dof])
        self.dofs.append(dof)

    def _dof_read_only_mask(self, read_only=None):
        return [dof.read_only == read_only if read_only is not None else True for dof in self.dofs]

    def _dof_active_mask(self, active=None):
        return [dof.active == active if active is not None else True for dof in self.dofs]

    def _dof_tags_mask(self, tags=[]):
        return [np.isin(dof["tags"], tags).any() if tags else True for dof in self.dofs]

    def _dof_mask(self, active=None, read_only=None, tags=[]):
        return [
            (k and m and t)
            for k, m, t in zip(self._dof_read_only_mask(read_only), self._dof_active_mask(active), self._dof_tags_mask(tags))
        ]

    def subset(self, active=None, read_only=None, tags=[]):
        return DOFList([dof for dof, m in zip(self.dofs, self._dof_mask(active, read_only, tags)) if m])

    def activate(self, read_only=None, active=None, tags=[]):
        for dof in self._subset_dofs(read_only, active, tags):
            dof.active = True

    def deactivate(self, read_only=None, active=None, tags=[]):
        for dof in self._subset_dofs(read_only, active, tags):
            dof.active = False


class BrownianMotion(SignalRO):
    """
    Read-only degree of freedom simulating brownian motion
    """

    def __init__(self, name=None, theta=0.95, *args, **kwargs):
        name = name if name is not None else str(uuid.uuid4())

        super().__init__(name=name, *args, **kwargs)

        self.theta = theta
        self.old_t = ttime.monotonic()
        self.old_y = 0.0

    def get(self):
        new_t = ttime.monotonic()
        alpha = self.theta ** (new_t - self.old_t)
        new_y = alpha * self.old_y + np.sqrt(1 - alpha**2) * np.random.standard_normal()

        self.old_t = new_t
        self.old_y = new_y
        return new_y


class TimeReadback(SignalRO):
    """
    Returns the current timestamp.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def get(self):
        return ttime.time()


class ConstantReadback(SignalRO):
    """
    Returns a constant every time you read it (more useful than you'd think).
    """

    def __init__(self, constant=1, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.constant = constant

    def get(self):
        return self.constant