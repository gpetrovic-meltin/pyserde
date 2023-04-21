"""
pyserde core module.
"""
import dataclasses
import enum
import functools
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, TypeVar, Union

import casefy
import jinja2
from typing_extensions import Type, get_type_hints

from .compat import (
    SerdeError,
    dataclass_fields,
    get_origin,
    has_default,
    has_default_factory,
    is_bare_dict,
    is_bare_list,
    is_bare_set,
    is_bare_tuple,
    is_class_var,
    is_dict,
    is_generic,
    is_list,
    is_literal,
    is_new_type_primitive,
    is_opt,
    is_set,
    is_tuple,
    is_union,
    is_variable_tuple,
    type_args,
    typename,
)
from .numpy import is_numpy_available, is_numpy_type

__all__ = ["Scope", "gen", "add_func", "Func", "Field", "fields", "FlattenOpts", "conv", "union_func_name"]

logger = logging.getLogger('serde')


# name of the serde context key
SERDE_SCOPE = '__serde__'

# main function keys
FROM_ITER = 'from_iter'
FROM_DICT = 'from_dict'
TO_ITER = 'to_iter'
TO_DICT = 'to_dict'
TYPE_CHECK = 'typecheck'

# prefixes used to distinguish the direction of a union function
UNION_SE_PREFIX = "union_se"
UNION_DE_PREFIX = "union_de"

LITERAL_DE_PREFIX = "literal_de"

SETTINGS = dict(debug=False)


def init(debug: bool = False) -> None:
    SETTINGS['debug'] = debug


@dataclass
class GlobalScope:
    """
    Container to store generated code for complext types e.g. Union.
    """

    classes: Dict[str, Type[Any]] = dataclasses.field(default_factory=dict)

    def get_union(self, cls: Type[Any]) -> Type[Any]:
        class_name = union_func_name("global", type_args(cls))
        return self.classes.get(class_name)

    def add_union(self, cls: Type[Any]) -> Type[Any]:
        from . import serde

        class_name = union_func_name("global", type_args(cls))
        wrapper_dataclass = dataclasses.make_dataclass(class_name, [("v", cls)])
        serde(wrapper_dataclass)
        self.classes[class_name] = wrapper_dataclass
        return wrapper_dataclass

    def serialize_union(self, cls: Type[Any], obj) -> Any:
        # print("serialize_union")
        wrapper_dataclass = self.get_union(cls)
        serde_scope: Scope = getattr(wrapper_dataclass, SERDE_SCOPE)
        func_name = union_func_name(UNION_SE_PREFIX, type_args(cls))
        # print(func_name, obj)
        return serde_scope.funcs[func_name](obj, False, False)

    def deserialize_union(self, cls: Type[Any], data) -> Any:
        # print("deserialize_union")
        wrapper_dataclass = self.get_union(cls)
        serde_scope: Scope = getattr(wrapper_dataclass, SERDE_SCOPE)
        func_name = union_func_name(UNION_DE_PREFIX, type_args(cls))
        # print(func_name, data)
        return serde_scope.funcs[func_name](cls=cls, data=data)


GLOBAL_SCOPE = GlobalScope()


@dataclass
class Scope:
    """
    Container to store types and functions used in code generation context.
    """

    cls: Type[Any]
    """ The exact class this scope is for (needed to distinguish scopes between inherited classes) """

    funcs: Dict[str, Callable] = dataclasses.field(default_factory=dict)
    """ Generated serialize and deserialize functions """

    defaults: Dict[str, Union[Callable, Any]] = dataclasses.field(default_factory=dict)
    """ Default values of the dataclass fields (factories & normal values) """

    code: Dict[str, str] = dataclasses.field(default_factory=dict)
    """ Generated source code (only filled when debug is True) """

    union_se_args: Dict[str, List[Type]] = dataclasses.field(default_factory=dict)
    """ The union serializing functions need references to their types """

    reuse_instances_default: bool = True
    """ Default values for to_dict & from_dict arguments """

    convert_sets_default: bool = False

    def __repr__(self) -> str:
        res: List[str] = []

        res.append('==================================================')
        res.append(self._justify(self.cls.__name__))
        res.append('==================================================')
        res.append('')

        if self.code:
            res.append('--------------------------------------------------')
            res.append(self._justify('Functions generated by pyserde'))
            res.append('--------------------------------------------------')
            res.extend([code for code in self.code.values()])
            res.append('')

        if self.funcs:
            res.append('--------------------------------------------------')
            res.append(self._justify('Function references in scope'))
            res.append('--------------------------------------------------')
            for k, v in self.funcs.items():
                res.append(f'{k}: {v}')
            res.append('')

        if self.defaults:
            res.append('--------------------------------------------------')
            res.append(self._justify('Default values for the dataclass fields'))
            res.append('--------------------------------------------------')
            for k, v in self.defaults.items():
                res.append(f'{k}: {v}')
            res.append('')

        if self.union_se_args:
            res.append('--------------------------------------------------')
            res.append(self._justify('Type list by used for union serialize functions'))
            res.append('--------------------------------------------------')
            for k, lst in self.union_se_args.items():
                res.append(f'{k}: {[v for v in lst]}')
            res.append('')

        return '\n'.join(res)

    def _justify(self, s: str, length=50) -> str:
        white_spaces = int((50 - len(s)) / 2)
        return ' ' * (white_spaces if white_spaces > 0 else 0) + s


def raise_unsupported_type(obj):
    # needed because we can not render a raise statement everywhere, e.g. as argument
    raise SerdeError(f"Unsupported type: {typename(type(obj))}")


def gen(code: str, globals: Dict = None, locals: Dict = None) -> str:
    """
    A wrapper of builtin `exec` function.
    """
    if SETTINGS['debug']:
        # black formatting is only important when debugging
        try:
            from black import FileMode, format_str

            code = format_str(code, mode=FileMode(line_length=100))
        except Exception:
            pass
    exec(code, globals, locals)
    return code


def add_func(serde_scope: Scope, func_name: str, func_code: str, globals: Dict) -> None:
    """
    Generate a function and add it to a Scope's `funcs` dictionary.

    * `serde_scope`: the Scope instance to modify
    * `func_name`: the name of the function
    * `func_code`: the source code of the function
    * `globals`: global variables that should be accessible to the generated function
    """

    code = gen(func_code, globals)
    serde_scope.funcs[func_name] = globals[func_name]

    if SETTINGS['debug']:
        serde_scope.code[func_name] = code


def is_instance(obj: Any, typ: Type) -> bool:
    """
    Type check function that works like `isinstance` but it accepts
    Subscripted Generics e.g. `List[int]`.
    """
    if dataclasses.is_dataclass(typ):
        serde_scope: Optional[Scope] = getattr(typ, SERDE_SCOPE, None)
        if serde_scope:
            try:
                serde_scope.funcs[TYPE_CHECK](obj)
            except Exception:
                return False
        return isinstance(obj, typ)
    elif is_opt(typ):
        return is_opt_instance(obj, typ)
    elif is_union(typ):
        return is_union_instance(obj, typ)
    elif is_list(typ):
        return is_list_instance(obj, typ)
    elif is_set(typ):
        return is_set_instance(obj, typ)
    elif is_tuple(typ):
        return is_tuple_instance(obj, typ)
    elif is_dict(typ):
        return is_dict_instance(obj, typ)
    elif is_generic(typ):
        return is_generic_instance(obj, typ)
    elif is_literal(typ):
        return True  # TODO
    elif is_new_type_primitive(typ):
        inner = getattr(typ, '__supertype__')
        return isinstance(obj, inner)
    elif typ is Ellipsis:
        return True
    else:
        return isinstance(obj, typ)


def is_opt_instance(obj: Any, typ: Type) -> bool:
    if obj is None:
        return True
    opt_arg = type_args(typ)[0]
    return is_instance(obj, opt_arg)


def is_union_instance(obj: Any, typ: Type) -> bool:
    for arg in type_args(typ):
        if is_instance(obj, arg):
            return True
    return False


def is_list_instance(obj: Any, typ: Type) -> bool:
    if not isinstance(obj, list):
        return False
    if len(obj) == 0 or is_bare_list(typ):
        return True
    list_arg = type_args(typ)[0]
    # for speed reasons we just check the type of the 1st element
    return is_instance(obj[0], list_arg)


def is_set_instance(obj: Any, typ: Type) -> bool:
    if not isinstance(obj, set):
        return False
    if len(obj) == 0 or is_bare_set(typ):
        return True
    set_arg = type_args(typ)[0]
    # for speed reasons we just check the type of the 1st element
    return is_instance(next(iter(obj)), set_arg)


def is_tuple_instance(obj: Any, typ: Type) -> bool:
    if not isinstance(obj, tuple):
        return False
    if is_variable_tuple(typ):
        arg = type_args(typ)[0]
        for v in obj:
            if not is_instance(v, arg):
                return False
    if len(obj) == 0 or is_bare_tuple(typ):
        return True
    for i, arg in enumerate(type_args(typ)):
        if not is_instance(obj[i], arg):
            return False
    return True


def is_dict_instance(obj: Any, typ: Type) -> bool:
    if not isinstance(obj, dict):
        return False
    if len(obj) == 0 or is_bare_dict(typ):
        return True
    ktyp = type_args(typ)[0]
    vtyp = type_args(typ)[1]
    for k, v in obj.items():
        # for speed reasons we just check the type of the 1st element
        return is_instance(k, ktyp) and is_instance(v, vtyp)
    return False


def is_generic_instance(obj: Any, typ: Type) -> bool:
    return is_instance(obj, get_origin(typ))


@dataclass
class Func:
    """
    Function wrapper that provides `mangled` optional field.

    pyserde copies every function reference into global scope
    for code generation. Mangling function names is needed in
    order to avoid name conflict in the global scope when
    multiple fields receives `skip_if` attribute.
    """

    inner: Callable
    """ Function to wrap in """

    mangeld: str = ""
    """ Mangled function name """

    def __call__(self, v):
        return self.inner(v)  # type: ignore

    @property
    def name(self) -> str:
        """
        Mangled function name
        """
        return self.mangeld


def skip_if_false(v):
    return not bool(v)


def skip_if_default(v, default=None):
    return v == default


@dataclass
class FlattenOpts:
    """
    Flatten options. Currently not used.
    """


def field(
    *args,
    rename: Optional[str] = None,
    alias: Optional[List[str]] = None,
    skip: Optional[bool] = None,
    skip_if: Optional[Callable] = None,
    skip_if_false: Optional[bool] = None,
    skip_if_default: Optional[bool] = None,
    serializer=None,
    deserializer=None,
    flatten: Optional[FlattenOpts] = None,
    metadata=None,
    **kwargs,
):
    """
    Declare a field with parameters.
    """
    if not metadata:
        metadata = {}

    if rename is not None:
        metadata["serde_rename"] = rename
    if alias is not None:
        metadata["serde_alias"] = alias
    if skip is not None:
        metadata["serde_skip"] = skip
    if skip_if is not None:
        metadata["serde_skip_if"] = skip_if
    if skip_if_false is not None:
        metadata["serde_skip_if_false"] = skip_if_false
    if skip_if_default is not None:
        metadata["serde_skip_if_default"] = skip_if_default
    if serializer:
        metadata["serde_serializer"] = serializer
    if deserializer:
        metadata["serde_deserializer"] = deserializer
    if flatten:
        metadata["serde_flatten"] = flatten

    return dataclasses.field(*args, metadata=metadata, **kwargs)


@dataclass
class Field:
    """
    Field class is similar to `dataclasses.Field`. It provides pyserde specific options.


    `type`, `name`, `default` and `default_factory` are the same members as `dataclasses.Field`.

    #### Field attributes

    Field attributes are options to customize (de)serialization behaviour specific to field. Field attributes
    can be specified through [metadata](https://docs.python.org/3/library/dataclasses.html#dataclasses.field)
    of `dataclasses.field`. dataclasses metadata is a container where users can pass arbitrary key and value.

    pyserde's field attributes have `serde` prefix to avoid conflicts with other libraries.

    ```python
    @deserialize
    @serialize
    @dataclass
    class Foo:
        i: int = field(metadata={"serde_<ATTRIBUTE_NAME>": <ATTRIBUTE_VALUE>})
    ```

    * `case` is an actual case name determined in regard with `rename_all` class attribute.
    This attribute is currently internal use only.

    * `rename` (Attribute name: `serde_rename`) is used to rename field name during (de)serialization. This attribute is
    convenient when you want to use a python keyword in field name. For example, this code renames `id` to `ID`.

    ```python
    @serialize
    @dataclass
    class Foo:
        id: int = field(metadata={"serde_rename": "ID"})
    ```

    * `skip` (Attribute name: `serde_skip`) is used to skip (de)serialization for a field.

    * `skip_if` (Attribute name: `serde_skip_if`) skips (de)serialization if the callable evaluates to `True`.

    * `skip_if_false` (Attribute name: `serde_skip_if_false`) skips (de)serialization if the field value evaluates
    to `False`. For example, this code skip (de)serialize `v` if `v` is empty.

    * `skip_if_default` (Attribute name: `serde_skip_if_default`) skips (de)serialization if the field value is equal
    to the default value

    ```python
    @deserialize
    @serialize
    @dataclass
    class Foo:
        v: List[int] = field(metadata={"serde_skip_if_false": True})
    ```

    * `serializer` (Attribute name: `serde_serializer`) takes a custom function to override the default serialization
    behaviour of a field.

    * `deserializer` (Attribute name: `serde_deserializer`) takes a custom function to override the default
    deserialization behaviour of a field.

    * `flatten` (Attribute name: `serde_flatten`) flattens the fields of the nested dataclass.

    """

    type: Type
    name: Optional[str]
    default: Any = field(default_factory=dataclasses._MISSING_TYPE)
    default_factory: Any = field(default_factory=dataclasses._MISSING_TYPE)
    init: bool = field(default_factory=dataclasses._MISSING_TYPE)
    repr: Any = field(default_factory=dataclasses._MISSING_TYPE)
    hash: Any = field(default_factory=dataclasses._MISSING_TYPE)
    compare: Any = field(default_factory=dataclasses._MISSING_TYPE)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    case: Optional[str] = None
    alias: List[str] = field(default_factory=list)
    rename: Optional[str] = None
    skip: Optional[bool] = None
    skip_if: Optional[Func] = None
    skip_if_false: Optional[bool] = None
    skip_if_default: Optional[bool] = None
    serializer: Optional[Func] = None  # Custom field serializer.
    deserializer: Optional[Func] = None  # Custom field deserializer.
    flatten: Optional[FlattenOpts] = None
    parent: Optional[Type] = None

    @classmethod
    def from_dataclass(cls, f: dataclasses.Field, parent: Optional[Type] = None) -> 'Field':
        """
        Create `Field` object from `dataclasses.Field`.
        """
        skip_if_false_func: Optional[Func] = None
        if f.metadata.get('serde_skip_if_false'):
            skip_if_false_func = Func(skip_if_false, cls.mangle(f, 'skip_if_false'))

        skip_if_default_func: Optional[Func] = None
        if f.metadata.get('serde_skip_if_default'):
            skip_if_def = functools.partial(skip_if_default, default=f.default)
            skip_if_default_func = Func(skip_if_def, cls.mangle(f, 'skip_if_default'))

        skip_if: Optional[Func] = None
        if f.metadata.get('serde_skip_if'):
            func = f.metadata.get('serde_skip_if')
            if callable(func):
                skip_if = Func(func, cls.mangle(f, 'skip_if'))

        serializer: Optional[Func] = None
        func = f.metadata.get('serde_serializer')
        if func:
            serializer = Func(func, cls.mangle(f, 'serializer'))

        deserializer: Optional[Func] = None
        func = f.metadata.get('serde_deserializer')
        if func:
            deserializer = Func(func, cls.mangle(f, 'deserializer'))

        flatten = f.metadata.get('serde_flatten')
        if flatten is True:
            flatten = FlattenOpts()

        return cls(
            f.type,
            f.name,
            default=f.default,
            default_factory=f.default_factory,  # type: ignore
            init=f.init,
            repr=f.repr,
            hash=f.hash,
            compare=f.compare,
            metadata=f.metadata,
            rename=f.metadata.get('serde_rename'),
            alias=f.metadata.get('serde_alias', []),
            skip=f.metadata.get('serde_skip'),
            skip_if=skip_if or skip_if_false_func or skip_if_default_func,
            serializer=serializer,
            deserializer=deserializer,
            flatten=flatten,
            parent=parent,
        )

    def to_dataclass(self) -> dataclasses.Field:
        f = dataclasses.Field(
            default=self.default,
            default_factory=self.default_factory,
            init=self.init,
            repr=self.repr,
            hash=self.hash,
            compare=self.compare,
            metadata=self.metadata,
        )
        assert self.name
        f.name = self.name
        f.type = self.type
        return f

    def is_self_referencing(self) -> bool:
        if self.type is None:
            return False
        if self.parent is None:
            return False
        return self.type == self.parent

    @staticmethod
    def mangle(field: dataclasses.Field, name: str) -> str:
        """
        Get mangled name based on field name.
        """
        return f'{field.name}_{name}'

    def conv_name(self, case: Optional[str] = None) -> str:
        """
        Get an actual field name which `rename` and `rename_all` conversions
        are made. Use `name` property to get a field name before conversion.
        """
        return conv(self, case or self.case)

    def supports_default(self) -> bool:
        return not getattr(self, "iterbased", False) and (has_default(self) or has_default_factory(self))


F = TypeVar('F', bound=Field)


def fields(field_cls: Type[F], cls: Type[Any], serialize_class_var: bool = False) -> List[F]:
    """
    Iterate fields of the dataclass and returns `serde.core.Field`.
    """
    fields = [field_cls.from_dataclass(f, parent=cls) for f in dataclass_fields(cls)]

    if serialize_class_var:
        for name, typ in get_type_hints(cls).items():
            if is_class_var(typ):
                fields.append(field_cls(typ, name, default=getattr(cls, name)))

    return fields


def conv(f: Field, case: Optional[str] = None) -> str:
    """
    Convert field name.
    """
    name = f.name
    if case:
        casef = getattr(casefy, case, None)
        if not casef:
            raise SerdeError(f"Unkown case type: {f.case}. Pass the name of case supported by 'casefy' package.")
        name = casef(name)
    if f.rename:
        name = f.rename
    if name is None:
        raise SerdeError('Field name is None.')
    return name


def union_func_name(prefix: str, union_args: List[Type[Any]]) -> str:
    """
    Generate a function name that contains all union types

    * `prefix` prefix to distinguish between serializing and deserializing
    * `union_args`: type arguments of a Union

    >>> from ipaddress import IPv4Address
    >>> from typing import List
    >>> union_func_name("union_se", [int, List[str], IPv4Address])
    'union_se_int_List_str__IPv4Address'
    """
    return re.sub(r"[^A-Za-z0-9]", "_", f"{prefix}_{'_'.join([typename(e) for e in union_args])}")


def literal_func_name(literal_args: List[Any]) -> str:
    """
    Generate a function name with all literals and corresponding types specified with Literal[...]


    * `literal_args`: arguments of a Literal

    >>> literal_func_name(["r", "w", "a", "x", "r+", "w+", "a+", "x+"])
    'literal_de_r_str_w_str_a_str_x_str_r__str_w__str_a__str_x__str'
    """
    return re.sub(
        r"[^A-Za-z0-9]", "_", f"{LITERAL_DE_PREFIX}_{'_'.join(f'{a}_{typename(type(a))}' for a in literal_args)}"
    )


@dataclass
class Tagging:
    """
    Controls how union is (de)serialized. This is the same concept as in
    https://serde.rs/enum-representations.html
    """

    class Kind(enum.Enum):
        External = enum.auto()
        Internal = enum.auto()
        Adjacent = enum.auto()
        Untagged = enum.auto()

    tag: Optional[str] = None
    content: Optional[str] = None
    kind: Kind = Kind.External

    def is_external(self):
        return self.kind == self.Kind.External

    def is_internal(self):
        return self.kind == self.Kind.Internal

    def is_adjacent(self):
        return self.kind == self.Kind.Adjacent

    def is_untagged(self):
        return self.kind == self.Kind.Untagged

    @classmethod
    def is_taggable(cls, typ):
        return dataclasses.is_dataclass(typ)

    def check(self):
        if self.is_internal() and self.tag is None:
            raise SerdeError("\"tag\" must be specified in InternalTagging")
        if self.is_adjacent() and (self.tag is None or self.content is None):
            raise SerdeError("\"tag\" and \"content\" must be specified in AdjacentTagging")


ExternalTagging = Tagging()

InternalTagging = functools.partial(Tagging, kind=Tagging.Kind.Internal)

AdjacentTagging = functools.partial(Tagging, kind=Tagging.Kind.Adjacent)

Untagged = Tagging(kind=Tagging.Kind.Untagged)

DefaultTagging = ExternalTagging


def ensure(expr, description):
    if not expr:
        raise Exception(description)


def should_impl_dataclass(cls):
    """
    Test if class doesn't have @dataclass.

    `dataclasses.is_dataclass` returns True even Derived class doesn't actually @dataclass.
    >>> @dataclasses.dataclass
    ... class Base:
    ...     a: int
    >>> class Derived(Base):
    ...     b: int
    >>> dataclasses.is_dataclass(Derived)
    True

    This function tells the class actually have @dataclass or not.
    >>> should_impl_dataclass(Base)
    False
    >>> should_impl_dataclass(Derived)
    True
    """
    if not dataclasses.is_dataclass(cls):
        return True

    annotations = getattr(cls, "__annotations__", {})
    if not annotations:
        return False

    if len(annotations) != len(dataclasses.fields(cls)):
        return True

    field_names = [field.name for field in dataclass_fields(cls)]
    for field_name in annotations:
        if field_name not in field_names:
            return True

    return False


def render_type_check(cls: Type) -> str:
    import serde.compat

    template = """
def {{type_check_func}}(self):
  {% for f in fields -%}

  {% if ((is_numpy_available() and is_numpy_type(f.type)) or
         compat.is_enum(f.type) or
         compat.is_literal(f.type)) %}

  {% elif is_dataclass(f.type) %}
  self.{{f.name}}.__serde__.funcs['{{type_check_func}}'](self.{{f.name}})

  {% elif (compat.is_set(f.type) or
           compat.is_list(f.type) or
           compat.is_dict(f.type) or
           compat.is_tuple(f.type) or
           compat.is_opt(f.type) or
           compat.is_primitive(f.type) or
           compat.is_str_serializable(f.type) or
           compat.is_datetime(f.type)) %}
  if not is_instance(self.{{f.name}}, {{f.type|typename}}):
    raise SerdeError(f"{{cls|typename}}.{{f.name}} is not instance of {{f.type|typename}}")

  {% endif %}
  {% endfor %}

  return
    """

    env = jinja2.Environment(loader=jinja2.DictLoader({'check': template}))
    env.filters.update({'typename': functools.partial(typename, with_typing_module=True)})
    return env.get_template('check').render(
        cls=cls,
        fields=dataclasses.fields(cls),
        compat=serde.compat,
        is_dataclass=dataclasses.is_dataclass,
        type_check_func=TYPE_CHECK,
        is_instance=is_instance,
        is_numpy_available=is_numpy_available,
        is_numpy_type=is_numpy_type,
    )


@dataclass
class TypeCheck:
    """
    Specify type check flavors.
    """

    class Kind(enum.Enum):
        NoCheck = enum.auto()
        """ No check performed """

        Coerce = enum.auto()
        """ Value is coerced into the declared type """
        Strict = enum.auto()
        """ Value are strictly checked against the declared type """

    kind: Kind

    def is_strict(self) -> bool:
        return self.kind == self.Kind.Strict

    def is_coerce(self) -> bool:
        return self.kind == self.Kind.Coerce

    def __call__(self, **kwargs) -> 'TypeCheck':
        # TODO
        return self


NoCheck = TypeCheck(kind=TypeCheck.Kind.NoCheck)

Coerce = TypeCheck(kind=TypeCheck.Kind.Coerce)

Strict = TypeCheck(kind=TypeCheck.Kind.Strict)


def coerce(typ: Type, obj: Any) -> Any:
    return typ(obj) if is_coercible(typ, obj) else obj


def is_coercible(typ: Type, obj: Any) -> bool:
    if obj is None:
        return False
    return True
