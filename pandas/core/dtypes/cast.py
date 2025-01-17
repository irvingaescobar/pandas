"""
Routines for casting.
"""
from contextlib import suppress
from datetime import datetime, timedelta
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Optional,
    Sequence,
    Set,
    Sized,
    Tuple,
    Type,
    Union,
    cast,
)
import warnings

import numpy as np

from pandas._libs import lib, missing as libmissing, tslib
from pandas._libs.tslibs import (
    NaT,
    OutOfBoundsDatetime,
    Period,
    Timedelta,
    Timestamp,
    conversion,
    iNaT,
    ints_to_pydatetime,
)
from pandas._libs.tslibs.timezones import tz_compare
from pandas._typing import AnyArrayLike, ArrayLike, Dtype, DtypeObj, Scalar
from pandas.util._validators import validate_bool_kwarg

from pandas.core.dtypes.common import (
    DT64NS_DTYPE,
    POSSIBLY_CAST_DTYPES,
    TD64NS_DTYPE,
    ensure_int8,
    ensure_int16,
    ensure_int32,
    ensure_int64,
    ensure_object,
    ensure_str,
    is_bool,
    is_bool_dtype,
    is_categorical_dtype,
    is_complex,
    is_complex_dtype,
    is_datetime64_dtype,
    is_datetime64_ns_dtype,
    is_datetime64tz_dtype,
    is_dtype_equal,
    is_extension_array_dtype,
    is_float,
    is_float_dtype,
    is_integer,
    is_integer_dtype,
    is_numeric_dtype,
    is_object_dtype,
    is_scalar,
    is_sparse,
    is_string_dtype,
    is_timedelta64_dtype,
    is_timedelta64_ns_dtype,
    is_unsigned_integer_dtype,
)
from pandas.core.dtypes.dtypes import (
    DatetimeTZDtype,
    ExtensionDtype,
    IntervalDtype,
    PeriodDtype,
)
from pandas.core.dtypes.generic import (
    ABCDataFrame,
    ABCExtensionArray,
    ABCIndex,
    ABCSeries,
)
from pandas.core.dtypes.inference import is_list_like
from pandas.core.dtypes.missing import is_valid_nat_for_dtype, isna, notna

if TYPE_CHECKING:
    from pandas import Series
    from pandas.core.arrays import DatetimeArray, ExtensionArray

_int8_max = np.iinfo(np.int8).max
_int16_max = np.iinfo(np.int16).max
_int32_max = np.iinfo(np.int32).max
_int64_max = np.iinfo(np.int64).max


def maybe_convert_platform(values):
    """ try to do platform conversion, allow ndarray or list here """
    if isinstance(values, (list, tuple, range)):
        values = construct_1d_object_array_from_listlike(values)
    if getattr(values, "dtype", None) == np.object_:
        if hasattr(values, "_values"):
            values = values._values
        values = lib.maybe_convert_objects(values)

    return values


def is_nested_object(obj) -> bool:
    """
    return a boolean if we have a nested object, e.g. a Series with 1 or
    more Series elements

    This may not be necessarily be performant.

    """
    if isinstance(obj, ABCSeries) and is_object_dtype(obj.dtype):

        if any(isinstance(v, ABCSeries) for v in obj._values):
            return True

    return False


def maybe_box_datetimelike(value: Scalar, dtype: Optional[Dtype] = None) -> Scalar:
    """
    Cast scalar to Timestamp or Timedelta if scalar is datetime-like
    and dtype is not object.

    Parameters
    ----------
    value : scalar
    dtype : Dtype, optional

    Returns
    -------
    scalar
    """
    if dtype == object:
        pass
    elif isinstance(value, (np.datetime64, datetime)):
        value = Timestamp(value)
    elif isinstance(value, (np.timedelta64, timedelta)):
        value = Timedelta(value)

    return value


def maybe_unbox_datetimelike(value: Scalar, dtype: DtypeObj) -> Scalar:
    """
    Convert a Timedelta or Timestamp to timedelta64 or datetime64 for setting
    into a numpy array.  Failing to unbox would risk dropping nanoseconds.

    Notes
    -----
    Caller is responsible for checking dtype.kind in ["m", "M"]
    """
    if is_valid_nat_for_dtype(value, dtype):
        # GH#36541: can't fill array directly with pd.NaT
        # > np.empty(10, dtype="datetime64[64]").fill(pd.NaT)
        # ValueError: cannot convert float NaN to integer
        value = dtype.type("NaT", "ns")
    elif isinstance(value, Timestamp):
        if value.tz is None:
            value = value.to_datetime64()
    elif isinstance(value, Timedelta):
        value = value.to_timedelta64()

    _disallow_mismatched_datetimelike(value, dtype)
    return value


def _disallow_mismatched_datetimelike(value: DtypeObj, dtype: DtypeObj):
    """
    numpy allows np.array(dt64values, dtype="timedelta64[ns]") and
    vice-versa, but we do not want to allow this, so we need to
    check explicitly
    """
    vdtype = getattr(value, "dtype", None)
    if vdtype is None:
        return
    elif (vdtype.kind == "m" and dtype.kind == "M") or (
        vdtype.kind == "M" and dtype.kind == "m"
    ):
        raise TypeError(f"Cannot cast {repr(value)} to {dtype}")


def maybe_downcast_to_dtype(result, dtype: Union[str, np.dtype]):
    """
    try to cast to the specified dtype (e.g. convert back to bool/int
    or could be an astype of float64->float32
    """
    do_round = False

    if is_scalar(result):
        return result
    elif isinstance(result, ABCDataFrame):
        # occurs in pivot_table doctest
        return result

    if isinstance(dtype, str):
        if dtype == "infer":
            inferred_type = lib.infer_dtype(ensure_object(result), skipna=False)
            if inferred_type == "boolean":
                dtype = "bool"
            elif inferred_type == "integer":
                dtype = "int64"
            elif inferred_type == "datetime64":
                dtype = "datetime64[ns]"
            elif inferred_type == "timedelta64":
                dtype = "timedelta64[ns]"

            # try to upcast here
            elif inferred_type == "floating":
                dtype = "int64"
                if issubclass(result.dtype.type, np.number):
                    do_round = True

            else:
                dtype = "object"

        dtype = np.dtype(dtype)

    elif dtype.type is Period:
        from pandas.core.arrays import PeriodArray

        with suppress(TypeError):
            # e.g. TypeError: int() argument must be a string, a
            #  bytes-like object or a number, not 'Period
            return PeriodArray(result, freq=dtype.freq)

    converted = maybe_downcast_numeric(result, dtype, do_round)
    if converted is not result:
        return converted

    # a datetimelike
    # GH12821, iNaT is cast to float
    if dtype.kind in ["M", "m"] and result.dtype.kind in ["i", "f"]:
        if isinstance(dtype, DatetimeTZDtype):
            # convert to datetime and change timezone
            i8values = result.astype("i8", copy=False)
            cls = dtype.construct_array_type()
            # equiv: DatetimeArray(i8values).tz_localize("UTC").tz_convert(dtype.tz)
            result = cls._simple_new(i8values, dtype=dtype)
        else:
            result = result.astype(dtype)

    return result


def maybe_downcast_numeric(result, dtype: DtypeObj, do_round: bool = False):
    """
    Subset of maybe_downcast_to_dtype restricted to numeric dtypes.

    Parameters
    ----------
    result : ndarray or ExtensionArray
    dtype : np.dtype or ExtensionDtype
    do_round : bool

    Returns
    -------
    ndarray or ExtensionArray
    """
    if not isinstance(dtype, np.dtype):
        # e.g. SparseDtype has no itemsize attr
        return result

    def trans(x):
        if do_round:
            return x.round()
        return x

    if dtype.kind == result.dtype.kind:
        # don't allow upcasts here (except if empty)
        if result.dtype.itemsize <= dtype.itemsize and result.size:
            return result

    if is_bool_dtype(dtype) or is_integer_dtype(dtype):

        if not result.size:
            # if we don't have any elements, just astype it
            return trans(result).astype(dtype)

        # do a test on the first element, if it fails then we are done
        r = result.ravel()
        arr = np.array([r[0]])

        if isna(arr).any():
            # if we have any nulls, then we are done
            return result

        elif not isinstance(r[0], (np.integer, np.floating, int, float, bool)):
            # a comparable, e.g. a Decimal may slip in here
            return result

        if (
            issubclass(result.dtype.type, (np.object_, np.number))
            and notna(result).all()
        ):
            new_result = trans(result).astype(dtype)
            if new_result.dtype.kind == "O" or result.dtype.kind == "O":
                # np.allclose may raise TypeError on object-dtype
                if (new_result == result).all():
                    return new_result
            else:
                if np.allclose(new_result, result, rtol=0):
                    return new_result

    elif (
        issubclass(dtype.type, np.floating)
        and not is_bool_dtype(result.dtype)
        and not is_string_dtype(result.dtype)
    ):
        return result.astype(dtype)

    return result


def maybe_cast_result(
    result: ArrayLike, obj: "Series", numeric_only: bool = False, how: str = ""
) -> ArrayLike:
    """
    Try casting result to a different type if appropriate

    Parameters
    ----------
    result : array-like
        Result to cast.
    obj : Series
        Input Series from which result was calculated.
    numeric_only : bool, default False
        Whether to cast only numerics or datetimes as well.
    how : str, default ""
        How the result was computed.

    Returns
    -------
    result : array-like
        result maybe casted to the dtype.
    """
    dtype = obj.dtype
    dtype = maybe_cast_result_dtype(dtype, how)

    assert not is_scalar(result)

    if (
        is_extension_array_dtype(dtype)
        and not is_categorical_dtype(dtype)
        and dtype.kind != "M"
    ):
        # We have to special case categorical so as not to upcast
        # things like counts back to categorical
        cls = dtype.construct_array_type()
        result = maybe_cast_to_extension_array(cls, result, dtype=dtype)

    elif numeric_only and is_numeric_dtype(dtype) or not numeric_only:
        result = maybe_downcast_to_dtype(result, dtype)

    return result


def maybe_cast_result_dtype(dtype: DtypeObj, how: str) -> DtypeObj:
    """
    Get the desired dtype of a result based on the
    input dtype and how it was computed.

    Parameters
    ----------
    dtype : DtypeObj
        Input dtype.
    how : str
        How the result was computed.

    Returns
    -------
    DtypeObj
        The desired dtype of the result.
    """
    from pandas.core.arrays.boolean import BooleanDtype
    from pandas.core.arrays.floating import Float64Dtype
    from pandas.core.arrays.integer import Int64Dtype, _IntegerDtype

    if how in ["add", "cumsum", "sum", "prod"]:
        if dtype == np.dtype(bool):
            return np.dtype(np.int64)
        elif isinstance(dtype, (BooleanDtype, _IntegerDtype)):
            return Int64Dtype()
    elif how in ["mean", "median", "var"] and isinstance(
        dtype, (BooleanDtype, _IntegerDtype)
    ):
        return Float64Dtype()
    return dtype


def maybe_cast_to_extension_array(
    cls: Type["ExtensionArray"], obj: ArrayLike, dtype: Optional[ExtensionDtype] = None
) -> ArrayLike:
    """
    Call to `_from_sequence` that returns the object unchanged on Exception.

    Parameters
    ----------
    cls : class, subclass of ExtensionArray
    obj : arraylike
        Values to pass to cls._from_sequence
    dtype : ExtensionDtype, optional

    Returns
    -------
    ExtensionArray or obj
    """
    from pandas.core.arrays.string_ import StringArray
    from pandas.core.arrays.string_arrow import ArrowStringArray

    assert isinstance(cls, type), f"must pass a type: {cls}"
    assertion_msg = f"must pass a subclass of ExtensionArray: {cls}"
    assert issubclass(cls, ABCExtensionArray), assertion_msg

    # Everything can be converted to StringArrays, but we may not want to convert
    if (
        issubclass(cls, (StringArray, ArrowStringArray))
        and lib.infer_dtype(obj) != "string"
    ):
        return obj

    try:
        result = cls._from_sequence(obj, dtype=dtype)
    except Exception:
        # We can't predict what downstream EA constructors may raise
        result = obj
    return result


def maybe_upcast_putmask(result: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """
    A safe version of putmask that potentially upcasts the result.

    The result is replaced with the first N elements of other,
    where N is the number of True values in mask.
    If the length of other is shorter than N, other will be repeated.

    Parameters
    ----------
    result : ndarray
        The destination array. This will be mutated in-place if no upcasting is
        necessary.
    mask : boolean ndarray

    Returns
    -------
    result : ndarray

    Examples
    --------
    >>> arr = np.arange(1, 6)
    >>> mask = np.array([False, True, False, True, True])
    >>> result = maybe_upcast_putmask(arr, mask)
    >>> result
    array([ 1., nan,  3., nan, nan])
    """
    if not isinstance(result, np.ndarray):
        raise ValueError("The result input must be a ndarray.")

    # NB: we never get here with result.dtype.kind in ["m", "M"]

    if mask.any():

        # we want to decide whether place will work
        # if we have nans in the False portion of our mask then we need to
        # upcast (possibly), otherwise we DON't want to upcast (e.g. if we
        # have values, say integers, in the success portion then it's ok to not
        # upcast)
        new_dtype, _ = maybe_promote(result.dtype, np.nan)
        if new_dtype != result.dtype:
            result = result.astype(new_dtype, copy=True)

        np.place(result, mask, np.nan)

    return result


def maybe_promote(dtype, fill_value=np.nan):
    """
    Find the minimal dtype that can hold both the given dtype and fill_value.

    Parameters
    ----------
    dtype : np.dtype or ExtensionDtype
    fill_value : scalar, default np.nan

    Returns
    -------
    dtype
        Upcasted from dtype argument if necessary.
    fill_value
        Upcasted from fill_value argument if necessary.

    Raises
    ------
    ValueError
        If fill_value is a non-scalar and dtype is not object.
    """
    if not is_scalar(fill_value) and not is_object_dtype(dtype):
        # with object dtype there is nothing to promote, and the user can
        #  pass pretty much any weird fill_value they like
        raise ValueError("fill_value must be a scalar")

    # if we passed an array here, determine the fill value by dtype
    if isinstance(fill_value, np.ndarray):
        if issubclass(fill_value.dtype.type, (np.datetime64, np.timedelta64)):
            fill_value = fill_value.dtype.type("NaT", "ns")
        else:

            # we need to change to object type as our
            # fill_value is of object type
            if fill_value.dtype == np.object_:
                dtype = np.dtype(np.object_)
            fill_value = np.nan

        if dtype == np.object_ or dtype.kind in ["U", "S"]:
            # We treat string-like dtypes as object, and _always_ fill
            #  with np.nan
            fill_value = np.nan
            dtype = np.dtype(np.object_)

    # returns tuple of (dtype, fill_value)
    if issubclass(dtype.type, np.datetime64):
        if isinstance(fill_value, datetime) and fill_value.tzinfo is not None:
            # Trying to insert tzaware into tznaive, have to cast to object
            dtype = np.dtype(np.object_)
        elif is_integer(fill_value) or (is_float(fill_value) and not isna(fill_value)):
            dtype = np.dtype(np.object_)
        elif is_valid_nat_for_dtype(fill_value, dtype):
            # e.g. pd.NA, which is not accepted by Timestamp constructor
            fill_value = np.datetime64("NaT", "ns")
        else:
            try:
                fill_value = Timestamp(fill_value).to_datetime64()
            except (TypeError, ValueError):
                dtype = np.dtype(np.object_)
    elif issubclass(dtype.type, np.timedelta64):
        if (
            is_integer(fill_value)
            or (is_float(fill_value) and not np.isnan(fill_value))
            or isinstance(fill_value, str)
        ):
            # TODO: What about str that can be a timedelta?
            dtype = np.dtype(np.object_)
        elif is_valid_nat_for_dtype(fill_value, dtype):
            # e.g pd.NA, which is not accepted by the  Timedelta constructor
            fill_value = np.timedelta64("NaT", "ns")
        else:
            try:
                fv = Timedelta(fill_value)
            except ValueError:
                dtype = np.dtype(np.object_)
            else:
                if fv is NaT:
                    # NaT has no `to_timedelta64` method
                    fill_value = np.timedelta64("NaT", "ns")
                else:
                    fill_value = fv.to_timedelta64()
    elif is_datetime64tz_dtype(dtype):
        if isna(fill_value):
            fill_value = NaT
        elif not isinstance(fill_value, datetime):
            dtype = np.dtype(np.object_)
        elif fill_value.tzinfo is None:
            dtype = np.dtype(np.object_)
        elif not tz_compare(fill_value.tzinfo, dtype.tz):
            # TODO: sure we want to cast here?
            dtype = np.dtype(np.object_)

    elif is_extension_array_dtype(dtype) and isna(fill_value):
        fill_value = dtype.na_value

    elif is_float(fill_value):
        if issubclass(dtype.type, np.bool_):
            dtype = np.dtype(np.object_)

        elif issubclass(dtype.type, np.integer):
            dtype = np.dtype(np.float64)

        elif dtype.kind == "f":
            mst = np.min_scalar_type(fill_value)
            if mst > dtype:
                # e.g. mst is np.float64 and dtype is np.float32
                dtype = mst

        elif dtype.kind == "c":
            mst = np.min_scalar_type(fill_value)
            dtype = np.promote_types(dtype, mst)

    elif is_bool(fill_value):
        if not issubclass(dtype.type, np.bool_):
            dtype = np.dtype(np.object_)

    elif is_integer(fill_value):
        if issubclass(dtype.type, np.bool_):
            dtype = np.dtype(np.object_)

        elif issubclass(dtype.type, np.integer):
            if not np.can_cast(fill_value, dtype):
                # upcast to prevent overflow
                mst = np.min_scalar_type(fill_value)
                dtype = np.promote_types(dtype, mst)
                if dtype.kind == "f":
                    # Case where we disagree with numpy
                    dtype = np.dtype(np.object_)

    elif is_complex(fill_value):
        if issubclass(dtype.type, np.bool_):
            dtype = np.dtype(np.object_)

        elif issubclass(dtype.type, (np.integer, np.floating)):
            mst = np.min_scalar_type(fill_value)
            dtype = np.promote_types(dtype, mst)

        elif dtype.kind == "c":
            mst = np.min_scalar_type(fill_value)
            if mst > dtype:
                # e.g. mst is np.complex128 and dtype is np.complex64
                dtype = mst

    elif fill_value is None or fill_value is libmissing.NA:
        # Note: we already excluded dt64/td64 dtypes above
        if is_float_dtype(dtype) or is_complex_dtype(dtype):
            fill_value = np.nan
        elif is_integer_dtype(dtype):
            dtype = np.float64
            fill_value = np.nan
        else:
            dtype = np.dtype(np.object_)
            if fill_value is not libmissing.NA:
                fill_value = np.nan
    else:
        dtype = np.dtype(np.object_)

    # in case we have a string that looked like a number
    if is_extension_array_dtype(dtype):
        pass
    elif issubclass(np.dtype(dtype).type, (bytes, str)):
        dtype = np.dtype(np.object_)

    fill_value = _ensure_dtype_type(fill_value, dtype)
    return dtype, fill_value


def _ensure_dtype_type(value, dtype: DtypeObj):
    """
    Ensure that the given value is an instance of the given dtype.

    e.g. if out dtype is np.complex64_, we should have an instance of that
    as opposed to a python complex object.

    Parameters
    ----------
    value : object
    dtype : np.dtype or ExtensionDtype

    Returns
    -------
    object
    """
    # Start with exceptions in which we do _not_ cast to numpy types
    if is_extension_array_dtype(dtype):
        return value
    elif dtype == np.object_:
        return value
    elif isna(value):
        # e.g. keep np.nan rather than try to cast to np.float32(np.nan)
        return value

    return dtype.type(value)


def infer_dtype_from(val, pandas_dtype: bool = False) -> Tuple[DtypeObj, Any]:
    """
    Interpret the dtype from a scalar or array.

    Parameters
    ----------
    val : object
    pandas_dtype : bool, default False
        whether to infer dtype including pandas extension types.
        If False, scalar/array belongs to pandas extension types is inferred as
        object
    """
    if not is_list_like(val):
        return infer_dtype_from_scalar(val, pandas_dtype=pandas_dtype)
    return infer_dtype_from_array(val, pandas_dtype=pandas_dtype)


def infer_dtype_from_scalar(val, pandas_dtype: bool = False) -> Tuple[DtypeObj, Any]:
    """
    Interpret the dtype from a scalar.

    Parameters
    ----------
    pandas_dtype : bool, default False
        whether to infer dtype including pandas extension types.
        If False, scalar belongs to pandas extension types is inferred as
        object
    """
    dtype: DtypeObj = np.dtype(object)

    # a 1-element ndarray
    if isinstance(val, np.ndarray):
        msg = "invalid ndarray passed to infer_dtype_from_scalar"
        if val.ndim != 0:
            raise ValueError(msg)

        dtype = val.dtype
        val = lib.item_from_zerodim(val)

    elif isinstance(val, str):

        # If we create an empty array using a string to infer
        # the dtype, NumPy will only allocate one character per entry
        # so this is kind of bad. Alternately we could use np.repeat
        # instead of np.empty (but then you still don't want things
        # coming out as np.str_!

        dtype = np.dtype(object)

    elif isinstance(val, (np.datetime64, datetime)):
        val = Timestamp(val)
        if val is NaT or val.tz is None:
            dtype = np.dtype("M8[ns]")
        else:
            if pandas_dtype:
                dtype = DatetimeTZDtype(unit="ns", tz=val.tz)
            else:
                # return datetimetz as object
                return np.dtype(object), val
        val = val.value

    elif isinstance(val, (np.timedelta64, timedelta)):
        val = Timedelta(val).value
        dtype = np.dtype("m8[ns]")

    elif is_bool(val):
        dtype = np.dtype(np.bool_)

    elif is_integer(val):
        if isinstance(val, np.integer):
            dtype = np.dtype(type(val))
        else:
            dtype = np.dtype(np.int64)

        try:
            np.array(val, dtype=dtype)
        except OverflowError:
            dtype = np.array(val).dtype

    elif is_float(val):
        if isinstance(val, np.floating):
            dtype = np.dtype(type(val))
        else:
            dtype = np.dtype(np.float64)

    elif is_complex(val):
        dtype = np.dtype(np.complex_)

    elif pandas_dtype:
        if lib.is_period(val):
            dtype = PeriodDtype(freq=val.freq)
        elif lib.is_interval(val):
            subtype = infer_dtype_from_scalar(val.left, pandas_dtype=True)[0]
            dtype = IntervalDtype(subtype=subtype)

    return dtype, val


def dict_compat(d: Dict[Scalar, Scalar]) -> Dict[Scalar, Scalar]:
    """
    Convert datetimelike-keyed dicts to a Timestamp-keyed dict.

    Parameters
    ----------
    d: dict-like object

    Returns
    -------
    dict

    """
    return {maybe_box_datetimelike(key): value for key, value in d.items()}


def infer_dtype_from_array(
    arr, pandas_dtype: bool = False
) -> Tuple[DtypeObj, ArrayLike]:
    """
    Infer the dtype from an array.

    Parameters
    ----------
    arr : array
    pandas_dtype : bool, default False
        whether to infer dtype including pandas extension types.
        If False, array belongs to pandas extension types
        is inferred as object

    Returns
    -------
    tuple (numpy-compat/pandas-compat dtype, array)

    Notes
    -----
    if pandas_dtype=False. these infer to numpy dtypes
    exactly with the exception that mixed / object dtypes
    are not coerced by stringifying or conversion

    if pandas_dtype=True. datetime64tz-aware/categorical
    types will retain there character.

    Examples
    --------
    >>> np.asarray([1, '1'])
    array(['1', '1'], dtype='<U21')

    >>> infer_dtype_from_array([1, '1'])
    (dtype('O'), [1, '1'])
    """
    if isinstance(arr, np.ndarray):
        return arr.dtype, arr

    if not is_list_like(arr):
        raise TypeError("'arr' must be list-like")

    if pandas_dtype and is_extension_array_dtype(arr):
        return arr.dtype, arr

    elif isinstance(arr, ABCSeries):
        return arr.dtype, np.asarray(arr)

    # don't force numpy coerce with nan's
    inferred = lib.infer_dtype(arr, skipna=False)
    if inferred in ["string", "bytes", "mixed", "mixed-integer"]:
        return (np.dtype(np.object_), arr)

    arr = np.asarray(arr)
    return arr.dtype, arr


def maybe_infer_dtype_type(element):
    """
    Try to infer an object's dtype, for use in arithmetic ops.

    Uses `element.dtype` if that's available.
    Objects implementing the iterator protocol are cast to a NumPy array,
    and from there the array's type is used.

    Parameters
    ----------
    element : object
        Possibly has a `.dtype` attribute, and possibly the iterator
        protocol.

    Returns
    -------
    tipo : type

    Examples
    --------
    >>> from collections import namedtuple
    >>> Foo = namedtuple("Foo", "dtype")
    >>> maybe_infer_dtype_type(Foo(np.dtype("i8")))
    dtype('int64')
    """
    tipo = None
    if hasattr(element, "dtype"):
        tipo = element.dtype
    elif is_list_like(element):
        element = np.asarray(element)
        tipo = element.dtype
    return tipo


def maybe_upcast(
    values: np.ndarray,
    fill_value: Scalar = np.nan,
    copy: bool = False,
) -> Tuple[np.ndarray, Scalar]:
    """
    Provide explicit type promotion and coercion.

    Parameters
    ----------
    values : np.ndarray
        The array that we may want to upcast.
    fill_value : what we want to fill with
    copy : bool, default True
        If True always make a copy even if no upcast is required.

    Returns
    -------
    values: np.ndarray
        the original array, possibly upcast
    fill_value:
        the fill value, possibly upcast
    """
    new_dtype, fill_value = maybe_promote(values.dtype, fill_value)
    # We get a copy in all cases _except_ (values.dtype == new_dtype and not copy)
    values = values.astype(new_dtype, copy=copy)

    return values, fill_value


def invalidate_string_dtypes(dtype_set: Set[DtypeObj]):
    """
    Change string like dtypes to object for
    ``DataFrame.select_dtypes()``.
    """
    non_string_dtypes = dtype_set - {np.dtype("S").type, np.dtype("<U").type}
    if non_string_dtypes != dtype_set:
        raise TypeError("string dtypes are not allowed, use 'object' instead")


def coerce_indexer_dtype(indexer, categories):
    """ coerce the indexer input array to the smallest dtype possible """
    length = len(categories)
    if length < _int8_max:
        return ensure_int8(indexer)
    elif length < _int16_max:
        return ensure_int16(indexer)
    elif length < _int32_max:
        return ensure_int32(indexer)
    return ensure_int64(indexer)


def astype_dt64_to_dt64tz(
    values: ArrayLike, dtype: DtypeObj, copy: bool, via_utc: bool = False
) -> "DatetimeArray":
    # GH#33401 we have inconsistent behaviors between
    #  Datetimeindex[naive].astype(tzaware)
    #  Series[dt64].astype(tzaware)
    # This collects them in one place to prevent further fragmentation.

    from pandas.core.construction import ensure_wrapped_if_datetimelike

    values = ensure_wrapped_if_datetimelike(values)
    values = cast("DatetimeArray", values)
    aware = isinstance(dtype, DatetimeTZDtype)

    if via_utc:
        # Series.astype behavior
        assert values.tz is None and aware  # caller is responsible for checking this
        dtype = cast(DatetimeTZDtype, dtype)

        if copy:
            # this should be the only copy
            values = values.copy()
        # FIXME: GH#33401 this doesn't match DatetimeArray.astype, which
        #  goes through the `not via_utc` path
        return values.tz_localize("UTC").tz_convert(dtype.tz)

    else:
        # DatetimeArray/DatetimeIndex.astype behavior

        if values.tz is None and aware:
            dtype = cast(DatetimeTZDtype, dtype)
            return values.tz_localize(dtype.tz)

        elif aware:
            # GH#18951: datetime64_tz dtype but not equal means different tz
            dtype = cast(DatetimeTZDtype, dtype)
            result = values.tz_convert(dtype.tz)
            if copy:
                result = result.copy()
            return result

        elif values.tz is not None and not aware:
            result = values.tz_convert("UTC").tz_localize(None)
            if copy:
                result = result.copy()
            return result

        raise NotImplementedError("dtype_equal case should be handled elsewhere")


def astype_td64_unit_conversion(
    values: np.ndarray, dtype: np.dtype, copy: bool
) -> np.ndarray:
    """
    By pandas convention, converting to non-nano timedelta64
    returns an int64-dtyped array with ints representing multiples
    of the desired timedelta unit.  This is essentially division.

    Parameters
    ----------
    values : np.ndarray[timedelta64[ns]]
    dtype : np.dtype
        timedelta64 with unit not-necessarily nano
    copy : bool

    Returns
    -------
    np.ndarray
    """
    if is_dtype_equal(values.dtype, dtype):
        if copy:
            return values.copy()
        return values

    # otherwise we are converting to non-nano
    result = values.astype(dtype, copy=False)  # avoid double-copying
    result = result.astype(np.float64)

    mask = isna(values)
    np.putmask(result, mask, np.nan)
    return result


def astype_nansafe(
    arr: np.ndarray, dtype: DtypeObj, copy: bool = True, skipna: bool = False
) -> ArrayLike:
    """
    Cast the elements of an array to a given dtype a nan-safe manner.

    Parameters
    ----------
    arr : ndarray
    dtype : np.dtype or ExtensionDtype
    copy : bool, default True
        If False, a view will be attempted but may fail, if
        e.g. the item sizes don't align.
    skipna: bool, default False
        Whether or not we should skip NaN when casting as a string-type.

    Raises
    ------
    ValueError
        The dtype was a datetime64/timedelta64 dtype, but it had no unit.
    """
    if arr.ndim > 1:
        # Make sure we are doing non-copy ravel and reshape.
        flags = arr.flags
        flat = arr.ravel("K")
        result = astype_nansafe(flat, dtype, copy=copy, skipna=skipna)
        order = "F" if flags.f_contiguous else "C"
        return result.reshape(arr.shape, order=order)

    # We get here with 0-dim from sparse
    arr = np.atleast_1d(arr)

    # dispatch on extension dtype if needed
    if isinstance(dtype, ExtensionDtype):
        return dtype.construct_array_type()._from_sequence(arr, dtype=dtype, copy=copy)

    elif not isinstance(dtype, np.dtype):
        raise ValueError("dtype must be np.dtype or ExtensionDtype")

    if arr.dtype.kind in ["m", "M"] and (
        issubclass(dtype.type, str) or dtype == object
    ):
        from pandas.core.construction import ensure_wrapped_if_datetimelike

        arr = ensure_wrapped_if_datetimelike(arr)
        return arr.astype(dtype, copy=copy)

    if issubclass(dtype.type, str):
        return lib.ensure_string_array(arr, skipna=skipna, convert_na_value=False)

    elif is_datetime64_dtype(arr):
        if dtype == np.int64:
            warnings.warn(
                f"casting {arr.dtype} values to int64 with .astype(...) "
                "is deprecated and will raise in a future version. "
                "Use .view(...) instead.",
                FutureWarning,
                # stacklevel chosen to be correct when reached via Series.astype
                stacklevel=7,
            )
            if isna(arr).any():
                raise ValueError("Cannot convert NaT values to integer")
            return arr.view(dtype)

        # allow frequency conversions
        if dtype.kind == "M":
            return arr.astype(dtype)

        raise TypeError(f"cannot astype a datetimelike from [{arr.dtype}] to [{dtype}]")

    elif is_timedelta64_dtype(arr):
        if dtype == np.int64:
            warnings.warn(
                f"casting {arr.dtype} values to int64 with .astype(...) "
                "is deprecated and will raise in a future version. "
                "Use .view(...) instead.",
                FutureWarning,
                # stacklevel chosen to be correct when reached via Series.astype
                stacklevel=7,
            )
            if isna(arr).any():
                raise ValueError("Cannot convert NaT values to integer")
            return arr.view(dtype)

        elif dtype.kind == "m":
            return astype_td64_unit_conversion(arr, dtype, copy=copy)

        raise TypeError(f"cannot astype a timedelta from [{arr.dtype}] to [{dtype}]")

    elif np.issubdtype(arr.dtype, np.floating) and np.issubdtype(dtype, np.integer):

        if not np.isfinite(arr).all():
            raise ValueError("Cannot convert non-finite values (NA or inf) to integer")

    elif is_object_dtype(arr):

        # work around NumPy brokenness, #1987
        if np.issubdtype(dtype.type, np.integer):
            return lib.astype_intsafe(arr, dtype)

        # if we have a datetime/timedelta array of objects
        # then coerce to a proper dtype and recall astype_nansafe

        elif is_datetime64_dtype(dtype):
            from pandas import to_datetime

            return astype_nansafe(to_datetime(arr).values, dtype, copy=copy)
        elif is_timedelta64_dtype(dtype):
            from pandas import to_timedelta

            return astype_nansafe(to_timedelta(arr)._values, dtype, copy=copy)

    if dtype.name in ("datetime64", "timedelta64"):
        msg = (
            f"The '{dtype.name}' dtype has no unit. Please pass in "
            f"'{dtype.name}[ns]' instead."
        )
        raise ValueError(msg)

    if copy or is_object_dtype(arr.dtype) or is_object_dtype(dtype):
        # Explicit copy, or required since NumPy can't view from / to object.
        return arr.astype(dtype, copy=True)

    return arr.astype(dtype, copy=copy)


def soft_convert_objects(
    values: np.ndarray,
    datetime: bool = True,
    numeric: bool = True,
    timedelta: bool = True,
    copy: bool = True,
):
    """
    Try to coerce datetime, timedelta, and numeric object-dtype columns
    to inferred dtype.

    Parameters
    ----------
    values : np.ndarray[object]
    datetime : bool, default True
    numeric: bool, default True
    timedelta : bool, default True
    copy : bool, default True

    Returns
    -------
    np.ndarray
    """
    validate_bool_kwarg(datetime, "datetime")
    validate_bool_kwarg(numeric, "numeric")
    validate_bool_kwarg(timedelta, "timedelta")
    validate_bool_kwarg(copy, "copy")

    conversion_count = sum((datetime, numeric, timedelta))
    if conversion_count == 0:
        raise ValueError("At least one of datetime, numeric or timedelta must be True.")

    # Soft conversions
    if datetime or timedelta:
        # GH 20380, when datetime is beyond year 2262, hence outside
        # bound of nanosecond-resolution 64-bit integers.
        try:
            values = lib.maybe_convert_objects(
                values, convert_datetime=datetime, convert_timedelta=timedelta
            )
        except OutOfBoundsDatetime:
            return values

    if numeric and is_object_dtype(values.dtype):
        converted = lib.maybe_convert_numeric(values, set(), coerce_numeric=True)

        # If all NaNs, then do not-alter
        values = converted if not isna(converted).all() else values
        values = values.copy() if copy else values

    return values


def convert_dtypes(
    input_array: AnyArrayLike,
    convert_string: bool = True,
    convert_integer: bool = True,
    convert_boolean: bool = True,
    convert_floating: bool = True,
) -> Dtype:
    """
    Convert objects to best possible type, and optionally,
    to types supporting ``pd.NA``.

    Parameters
    ----------
    input_array : ExtensionArray, Index, Series or np.ndarray
    convert_string : bool, default True
        Whether object dtypes should be converted to ``StringDtype()``.
    convert_integer : bool, default True
        Whether, if possible, conversion can be done to integer extension types.
    convert_boolean : bool, defaults True
        Whether object dtypes should be converted to ``BooleanDtypes()``.
    convert_floating : bool, defaults True
        Whether, if possible, conversion can be done to floating extension types.
        If `convert_integer` is also True, preference will be give to integer
        dtypes if the floats can be faithfully casted to integers.

    Returns
    -------
    dtype
        new dtype
    """
    is_extension = is_extension_array_dtype(input_array.dtype)
    if (
        convert_string or convert_integer or convert_boolean or convert_floating
    ) and not is_extension:
        try:
            inferred_dtype = lib.infer_dtype(input_array)
        except ValueError:
            # Required to catch due to Period.  Can remove once GH 23553 is fixed
            inferred_dtype = input_array.dtype

        if not convert_string and is_string_dtype(inferred_dtype):
            inferred_dtype = input_array.dtype

        if convert_integer:
            target_int_dtype = "Int64"

            if is_integer_dtype(input_array.dtype):
                from pandas.core.arrays.integer import INT_STR_TO_DTYPE

                inferred_dtype = INT_STR_TO_DTYPE.get(
                    input_array.dtype.name, target_int_dtype
                )
            if not is_integer_dtype(input_array.dtype) and is_numeric_dtype(
                input_array.dtype
            ):
                inferred_dtype = target_int_dtype

        else:
            if is_integer_dtype(inferred_dtype):
                inferred_dtype = input_array.dtype

        if convert_floating:
            if not is_integer_dtype(input_array.dtype) and is_numeric_dtype(
                input_array.dtype
            ):
                from pandas.core.arrays.floating import FLOAT_STR_TO_DTYPE

                inferred_float_dtype = FLOAT_STR_TO_DTYPE.get(
                    input_array.dtype.name, "Float64"
                )
                # if we could also convert to integer, check if all floats
                # are actually integers
                if convert_integer:
                    arr = input_array[notna(input_array)]
                    if (arr.astype(int) == arr).all():
                        inferred_dtype = "Int64"
                    else:
                        inferred_dtype = inferred_float_dtype
                else:
                    inferred_dtype = inferred_float_dtype
        else:
            if is_float_dtype(inferred_dtype):
                inferred_dtype = input_array.dtype

        if convert_boolean:
            if is_bool_dtype(input_array.dtype):
                inferred_dtype = "boolean"
        else:
            if isinstance(inferred_dtype, str) and inferred_dtype == "boolean":
                inferred_dtype = input_array.dtype

    else:
        inferred_dtype = input_array.dtype

    return inferred_dtype


def maybe_castable(arr: np.ndarray) -> bool:
    # return False to force a non-fastpath

    assert isinstance(arr, np.ndarray)  # GH 37024

    # check datetime64[ns]/timedelta64[ns] are valid
    # otherwise try to coerce
    kind = arr.dtype.kind
    if kind == "M":
        return is_datetime64_ns_dtype(arr.dtype)
    elif kind == "m":
        return is_timedelta64_ns_dtype(arr.dtype)

    return arr.dtype.name not in POSSIBLY_CAST_DTYPES


def maybe_infer_to_datetimelike(
    value: Union[ArrayLike, Scalar], convert_dates: bool = False
):
    """
    we might have a array (or single object) that is datetime like,
    and no dtype is passed don't change the value unless we find a
    datetime/timedelta set

    this is pretty strict in that a datetime/timedelta is REQUIRED
    in addition to possible nulls/string likes

    Parameters
    ----------
    value : np.array / Series / Index / list-like
    convert_dates : bool, default False
       if True try really hard to convert dates (such as datetime.date), other
       leave inferred dtype 'date' alone

    """
    if isinstance(value, (ABCIndex, ABCExtensionArray)):
        if not is_object_dtype(value.dtype):
            raise ValueError("array-like value must be object-dtype")

    v = value

    if not is_list_like(v):
        v = [v]
    v = np.array(v, copy=False)

    # we only care about object dtypes
    if not is_object_dtype(v):
        return value

    shape = v.shape
    if v.ndim != 1:
        v = v.ravel()

    if not len(v):
        return value

    def try_datetime(v):
        # safe coerce to datetime64
        try:
            # GH19671
            # tznaive only
            v = tslib.array_to_datetime(v, require_iso8601=True, errors="raise")[0]
        except ValueError:

            # we might have a sequence of the same-datetimes with tz's
            # if so coerce to a DatetimeIndex; if they are not the same,
            # then these stay as object dtype, xref GH19671
            from pandas import DatetimeIndex

            try:

                values, tz = conversion.datetime_to_datetime64(v)
            except (ValueError, TypeError):
                pass
            else:
                return DatetimeIndex(values).tz_localize("UTC").tz_convert(tz=tz)
        except TypeError:
            # e.g. <class 'numpy.timedelta64'> is not convertible to datetime
            pass

        return v.reshape(shape)

    def try_timedelta(v):
        # safe coerce to timedelta64

        # will try first with a string & object conversion
        from pandas import to_timedelta

        try:
            td_values = to_timedelta(v)
        except ValueError:
            return v.reshape(shape)
        else:
            return np.asarray(td_values).reshape(shape)

    inferred_type = lib.infer_datetimelike_array(ensure_object(v))

    if inferred_type == "date" and convert_dates:
        value = try_datetime(v)
    elif inferred_type == "datetime":
        value = try_datetime(v)
    elif inferred_type == "timedelta":
        value = try_timedelta(v)
    elif inferred_type == "nat":

        # if all NaT, return as datetime
        if isna(v).all():
            value = try_datetime(v)
        else:

            # We have at least a NaT and a string
            # try timedelta first to avoid spurious datetime conversions
            # e.g. '00:00:01' is a timedelta but technically is also a datetime
            value = try_timedelta(v)
            if lib.infer_dtype(value, skipna=False) in ["mixed"]:
                # cannot skip missing values, as NaT implies that the string
                # is actually a datetime
                value = try_datetime(v)

    return value


def maybe_cast_to_datetime(value, dtype: Optional[DtypeObj]):
    """
    try to cast the array/value to a datetimelike dtype, converting float
    nan to iNaT
    """
    from pandas.core.tools.datetimes import to_datetime
    from pandas.core.tools.timedeltas import to_timedelta

    if not is_list_like(value):
        raise TypeError("value must be listlike")

    if dtype is not None:
        is_datetime64 = is_datetime64_dtype(dtype)
        is_datetime64tz = is_datetime64tz_dtype(dtype)
        is_timedelta64 = is_timedelta64_dtype(dtype)

        if is_datetime64 or is_datetime64tz or is_timedelta64:

            # Force the dtype if needed.
            msg = (
                f"The '{dtype.name}' dtype has no unit. "
                f"Please pass in '{dtype.name}[ns]' instead."
            )

            if is_datetime64:
                # unpack e.g. SparseDtype
                dtype = getattr(dtype, "subtype", dtype)
                if not is_dtype_equal(dtype, DT64NS_DTYPE):

                    # pandas supports dtype whose granularity is less than [ns]
                    # e.g., [ps], [fs], [as]
                    if dtype <= np.dtype("M8[ns]"):
                        if dtype.name == "datetime64":
                            raise ValueError(msg)
                        dtype = DT64NS_DTYPE
                    else:
                        raise TypeError(
                            f"cannot convert datetimelike to dtype [{dtype}]"
                        )

            elif is_timedelta64 and not is_dtype_equal(dtype, TD64NS_DTYPE):

                # pandas supports dtype whose granularity is less than [ns]
                # e.g., [ps], [fs], [as]
                if dtype <= np.dtype("m8[ns]"):
                    if dtype.name == "timedelta64":
                        raise ValueError(msg)
                    dtype = TD64NS_DTYPE
                else:
                    raise TypeError(f"cannot convert timedeltalike to dtype [{dtype}]")

            if not is_sparse(value):
                value = np.array(value, copy=False)

                # have a scalar array-like (e.g. NaT)
                if value.ndim == 0:
                    value = iNaT

                # we have an array of datetime or timedeltas & nulls
                elif np.prod(value.shape) or not is_dtype_equal(value.dtype, dtype):
                    try:
                        if is_datetime64:
                            value = to_datetime(value, errors="raise")
                            # GH 25843: Remove tz information since the dtype
                            # didn't specify one
                            if value.tz is not None:
                                value = value.tz_localize(None)
                            value = value._values
                        elif is_datetime64tz:
                            # The string check can be removed once issue #13712
                            # is solved. String data that is passed with a
                            # datetime64tz is assumed to be naive which should
                            # be localized to the timezone.
                            is_dt_string = is_string_dtype(value.dtype)
                            value = to_datetime(value, errors="raise").array
                            if is_dt_string:
                                # Strings here are naive, so directly localize
                                value = value.tz_localize(dtype.tz)
                            else:
                                # Numeric values are UTC at this point,
                                # so localize and convert
                                value = value.tz_localize("UTC").tz_convert(dtype.tz)
                        elif is_timedelta64:
                            value = to_timedelta(value, errors="raise")._values
                    except OutOfBoundsDatetime:
                        raise
                    except (ValueError, TypeError):
                        pass

        # coerce datetimelike to object
        elif is_datetime64_dtype(
            getattr(value, "dtype", None)
        ) and not is_datetime64_dtype(dtype):
            if is_object_dtype(dtype):
                if value.dtype != DT64NS_DTYPE:
                    value = value.astype(DT64NS_DTYPE)
                ints = np.asarray(value).view("i8")
                return ints_to_pydatetime(ints)

            # we have a non-castable dtype that was passed
            raise TypeError(f"Cannot cast datetime64 to {dtype}")

    else:

        is_array = isinstance(value, np.ndarray)

        # catch a datetime/timedelta that is not of ns variety
        # and no coercion specified
        if is_array and value.dtype.kind in ["M", "m"]:
            value = sanitize_to_nanoseconds(value)

        # only do this if we have an array and the dtype of the array is not
        # setup already we are not an integer/object, so don't bother with this
        # conversion
        elif not (
            is_array
            and not (
                issubclass(value.dtype.type, np.integer) or value.dtype == np.object_
            )
        ):
            value = maybe_infer_to_datetimelike(value)

    return value


def sanitize_to_nanoseconds(values: np.ndarray) -> np.ndarray:
    """
    Safely convert non-nanosecond datetime64 or timedelta64 values to nanosecond.
    """
    dtype = values.dtype
    if dtype.kind == "M" and dtype != DT64NS_DTYPE:
        values = conversion.ensure_datetime64ns(values)

    elif dtype.kind == "m" and dtype != TD64NS_DTYPE:
        values = conversion.ensure_timedelta64ns(values)

    return values


def find_common_type(types: List[DtypeObj]) -> DtypeObj:
    """
    Find a common data type among the given dtypes.

    Parameters
    ----------
    types : list of dtypes

    Returns
    -------
    pandas extension or numpy dtype

    See Also
    --------
    numpy.find_common_type

    """
    if len(types) == 0:
        raise ValueError("no types given")

    first = types[0]

    # workaround for find_common_type([np.dtype('datetime64[ns]')] * 2)
    # => object
    if all(is_dtype_equal(first, t) for t in types[1:]):
        return first

    # get unique types (dict.fromkeys is used as order-preserving set())
    types = list(dict.fromkeys(types).keys())

    if any(isinstance(t, ExtensionDtype) for t in types):
        for t in types:
            if isinstance(t, ExtensionDtype):
                res = t._get_common_dtype(types)
                if res is not None:
                    return res
        return np.dtype("object")

    # take lowest unit
    if all(is_datetime64_dtype(t) for t in types):
        return np.dtype("datetime64[ns]")
    if all(is_timedelta64_dtype(t) for t in types):
        return np.dtype("timedelta64[ns]")

    # don't mix bool / int or float or complex
    # this is different from numpy, which casts bool with float/int as int
    has_bools = any(is_bool_dtype(t) for t in types)
    if has_bools:
        for t in types:
            if is_integer_dtype(t) or is_float_dtype(t) or is_complex_dtype(t):
                return np.dtype("object")

    return np.find_common_type(types, [])


def construct_2d_arraylike_from_scalar(
    value: Scalar, length: int, width: int, dtype: np.dtype, copy: bool
) -> np.ndarray:

    if dtype.kind in ["m", "M"]:
        value = maybe_unbox_datetimelike(value, dtype)

    # Attempt to coerce to a numpy array
    try:
        arr = np.array(value, dtype=dtype, copy=copy)
    except (ValueError, TypeError) as err:
        raise TypeError(
            f"DataFrame constructor called with incompatible data and dtype: {err}"
        ) from err

    if arr.ndim != 0:
        raise ValueError("DataFrame constructor not properly called!")

    shape = (length, width)
    return np.full(shape, arr)


def construct_1d_arraylike_from_scalar(
    value: Scalar, length: int, dtype: Optional[DtypeObj]
) -> ArrayLike:
    """
    create a np.ndarray / pandas type of specified shape and dtype
    filled with values

    Parameters
    ----------
    value : scalar value
    length : int
    dtype : pandas_dtype or np.dtype

    Returns
    -------
    np.ndarray / pandas type of length, filled with value

    """

    if dtype is None:
        try:
            dtype, value = infer_dtype_from_scalar(value, pandas_dtype=True)
        except OutOfBoundsDatetime:
            dtype = np.dtype(object)

    if is_extension_array_dtype(dtype):
        cls = dtype.construct_array_type()
        subarr = cls._from_sequence([value] * length, dtype=dtype)

    else:

        if length and is_integer_dtype(dtype) and isna(value):
            # coerce if we have nan for an integer dtype
            dtype = np.dtype("float64")
        elif isinstance(dtype, np.dtype) and dtype.kind in ("U", "S"):
            # we need to coerce to object dtype to avoid
            # to allow numpy to take our string as a scalar value
            dtype = np.dtype("object")
            if not isna(value):
                value = ensure_str(value)
        elif dtype.kind in ["M", "m"]:
            value = maybe_unbox_datetimelike(value, dtype)

        subarr = np.empty(length, dtype=dtype)
        subarr.fill(value)

    return subarr


def construct_1d_object_array_from_listlike(values: Sized) -> np.ndarray:
    """
    Transform any list-like object in a 1-dimensional numpy array of object
    dtype.

    Parameters
    ----------
    values : any iterable which has a len()

    Raises
    ------
    TypeError
        * If `values` does not have a len()

    Returns
    -------
    1-dimensional numpy array of dtype object
    """
    # numpy will try to interpret nested lists as further dimensions, hence
    # making a 1D array that contains list-likes is a bit tricky:
    result = np.empty(len(values), dtype="object")
    result[:] = values
    return result


def construct_1d_ndarray_preserving_na(
    values: Sequence, dtype: Optional[DtypeObj] = None, copy: bool = False
) -> np.ndarray:
    """
    Construct a new ndarray, coercing `values` to `dtype`, preserving NA.

    Parameters
    ----------
    values : Sequence
    dtype : numpy.dtype, optional
    copy : bool, default False
        Note that copies may still be made with ``copy=False`` if casting
        is required.

    Returns
    -------
    arr : ndarray[dtype]

    Examples
    --------
    >>> np.array([1.0, 2.0, None], dtype='str')
    array(['1.0', '2.0', 'None'], dtype='<U4')

    >>> construct_1d_ndarray_preserving_na([1.0, 2.0, None], dtype=np.dtype('str'))
    array(['1.0', '2.0', None], dtype=object)
    """

    if dtype is not None and dtype.kind == "U":
        subarr = lib.ensure_string_array(values, convert_na_value=False, copy=copy)
    else:
        if dtype is not None:
            _disallow_mismatched_datetimelike(values, dtype)
        subarr = np.array(values, dtype=dtype, copy=copy)

    return subarr


def maybe_cast_to_integer_array(arr, dtype: Dtype, copy: bool = False):
    """
    Takes any dtype and returns the casted version, raising for when data is
    incompatible with integer/unsigned integer dtypes.

    .. versionadded:: 0.24.0

    Parameters
    ----------
    arr : array-like
        The array to cast.
    dtype : str, np.dtype
        The integer dtype to cast the array to.
    copy: bool, default False
        Whether to make a copy of the array before returning.

    Returns
    -------
    ndarray
        Array of integer or unsigned integer dtype.

    Raises
    ------
    OverflowError : the dtype is incompatible with the data
    ValueError : loss of precision has occurred during casting

    Examples
    --------
    If you try to coerce negative values to unsigned integers, it raises:

    >>> pd.Series([-1], dtype="uint64")
    Traceback (most recent call last):
        ...
    OverflowError: Trying to coerce negative values to unsigned integers

    Also, if you try to coerce float values to integers, it raises:

    >>> pd.Series([1, 2, 3.5], dtype="int64")
    Traceback (most recent call last):
        ...
    ValueError: Trying to coerce float values to integers
    """
    assert is_integer_dtype(dtype)

    try:
        if not hasattr(arr, "astype"):
            casted = np.array(arr, dtype=dtype, copy=copy)
        else:
            casted = arr.astype(dtype, copy=copy)
    except OverflowError as err:
        raise OverflowError(
            "The elements provided in the data cannot all be "
            f"casted to the dtype {dtype}"
        ) from err

    if np.array_equal(arr, casted):
        return casted

    # We do this casting to allow for proper
    # data and dtype checking.
    #
    # We didn't do this earlier because NumPy
    # doesn't handle `uint64` correctly.
    arr = np.asarray(arr)

    if is_unsigned_integer_dtype(dtype) and (arr < 0).any():
        raise OverflowError("Trying to coerce negative values to unsigned integers")

    if is_float_dtype(arr) or is_object_dtype(arr):
        raise ValueError("Trying to coerce float values to integers")


def convert_scalar_for_putitemlike(scalar: Scalar, dtype: np.dtype) -> Scalar:
    """
    Convert datetimelike scalar if we are setting into a datetime64
    or timedelta64 ndarray.

    Parameters
    ----------
    scalar : scalar
    dtype : np.dtype

    Returns
    -------
    scalar
    """
    if dtype.kind in ["m", "M"]:
        scalar = maybe_box_datetimelike(scalar, dtype)
        return maybe_unbox_datetimelike(scalar, dtype)
    else:
        validate_numeric_casting(dtype, scalar)
    return scalar


def validate_numeric_casting(dtype: np.dtype, value: Scalar) -> None:
    """
    Check that we can losslessly insert the given value into an array
    with the given dtype.

    Parameters
    ----------
    dtype : np.dtype
    value : scalar

    Raises
    ------
    ValueError
    """
    if issubclass(dtype.type, (np.integer, np.bool_)):
        if is_float(value) and np.isnan(value):
            raise ValueError("Cannot assign nan to integer series")

    if issubclass(dtype.type, (np.integer, np.floating, complex)) and not issubclass(
        dtype.type, np.bool_
    ):
        if is_bool(value):
            raise ValueError("Cannot assign bool to float/integer series")
