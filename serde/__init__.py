"""
.. include:: ../README.md

## Modules

The following modules provide the core functionalities of `pyserde`.
* `serde.se`: All about serialization.
* `serde.de`: All about deserialization.
* `serde.core`: Core module used by `serde.se` and `serde.de` modules.
* `serde.compat`: Compatibility layer which handles mostly differences of `typing` module between python versions.

The following modules provide pyserde's (de)serialize APIs.
* `serde.json`: Serialize and Deserialize in JSON.
* `serde.msgpack`: Serialize and Deserialize in MsgPack.
* `serde.yaml`: Serialize and Deserialize in YAML.
* `serde.toml`: Serialize and Deserialize in TOML.
* `serde.pickle`: Serialize and Deserialize in Pickle.

Other modules
* `serde.inspect`: Prints generated code by pyserde.
"""

from dataclasses import dataclass
from typing import Callable, Optional, Type, overload

from typing_extensions import dataclass_transform

from .compat import SerdeError, SerdeSkip, T
from .core import (
    AdjacentTagging,
    Coerce,
    DefaultTagging,
    ExternalTagging,
    InternalTagging,
    NoCheck,
    Strict,
    Tagging,
    TypeCheck,
    Untagged,
    field,
    init,
    logger,
    should_impl_dataclass,
)
from .de import DeserializeFunc, default_deserializer, deserialize, from_dict, from_tuple, is_deserializable
from .se import SerializeFunc, asdict, astuple, default_serializer, is_serializable, serialize, to_dict, to_tuple

__all__ = [
    "serde",
    "serialize",
    "deserialize",
    "is_serializable",
    "is_deserializable",
    "to_dict",
    "from_dict",
    "to_tuple",
    "from_tuple",
    "SerdeError",
    "SerdeSkip",
    "AdjacentTagging",
    "ExternalTagging",
    "InternalTagging",
    "Untagged",
    "NoCheck",
    "Strict",
    "Coerce",
    "field",
    "default_deserializer",
    "asdict",
    "astuple",
    "default_serializer",
    "compat",
    "core",
    "de",
    "inspect",
    "json",
    "msgpack",
    "numpy",
    "se",
    "toml",
    "pickle",
    "yaml",
    "init",
    "logger",
]


@overload
def serde(
    _cls: Type[T],
    rename_all: Optional[str] = None,
    reuse_instances_default: bool = True,
    convert_sets_default: bool = False,
    serializer: Optional[SerializeFunc] = None,
    deserializer: Optional[DeserializeFunc] = None,
    tagging: Tagging = DefaultTagging,
    type_check: TypeCheck = NoCheck,
    serialize_class_var: bool = False,
) -> Type[T]:
    ...


@overload
def serde(
    rename_all: Optional[str] = None,
    reuse_instances_default: bool = True,
    convert_sets_default: bool = False,
    serializer: Optional[SerializeFunc] = None,
    deserializer: Optional[DeserializeFunc] = None,
    tagging: Tagging = DefaultTagging,
    type_check: TypeCheck = NoCheck,
    serialize_class_var: bool = False,
) -> Callable[[Type[T]], Type[T]]:
    ...


@dataclass_transform()
def serde(
    _cls=None,
    **kwargs,
):  # type: ignore
    """
    serde decorator. Keyword arguments are passed in `serialize` and `deserialize`.
    """

    def wrap(cls):
        if should_impl_dataclass(cls):
            dataclass(cls)
        serialize(cls, **kwargs)
        deserialize(cls, **kwargs)
        return cls

    if _cls is None:
        return wrap  # type: ignore

    return wrap(_cls)
