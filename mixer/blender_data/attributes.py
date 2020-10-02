# GPLv3 License
#
# Copyright (C) 2020 Ubisoft
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from __future__ import annotations

from enum import IntEnum
import logging
import traceback
from typing import Any, Optional, Union, TYPE_CHECKING

import bpy
import bpy.types as T  # noqa

from mixer.blender_data.blenddata import bl_rna_to_type
from mixer.blender_data.proxy import Delta, DeltaUpdate, Proxy
from mixer.blender_data.types import is_builtin, is_vector, is_matrix

if TYPE_CHECKING:
    from mixer.blender_data.bpy_data_proxy import RootIds, VisitState

logger = logging.getLogger(__name__)


class LoadElementAs(IntEnum):
    STRUCT = 0
    ID_REF = 1
    ID_DEF = 2


def same_rna(a, b):
    return a.bl_rna == b.bl_rna


def is_ID_subclass_rna(bl_rna):  # noqa
    """
    Return true if the RNA is of a subclass of bpy.types.ID
    """
    return issubclass(bl_rna_to_type(bl_rna), bpy.types.ID)


def load_as_what(attr_property: bpy.types.Property, attr: Any, root_ids: RootIds):
    """
    Determine if we must load an attribute as a struct, a blenddata collection element (ID_DEF)
    or a reference to a BlendData collection element (ID_REF)

    All T.Struct are loaded as struct
    All T.ID are loaded ad IDRef (that is pointer into a D.blendata collection) except
    for specific case. For instance the scene master "collection" is not a D.collections item.

    Arguments
    parent -- the type that contains the attribute names attr_name, for instance T.Scene
    attr_property -- a bl_rna property of a attribute, that can be a CollectionProperty or a "plain" attribute
    """
    # Reference to an ID element at the root of the blend file
    if attr in root_ids:
        return LoadElementAs.ID_REF

    if same_rna(attr_property, T.CollectionProperty):
        if is_ID_subclass_rna(attr_property.fixed_type.bl_rna):
            # Collections at root level are not handled by this code (See BpyDataProxy.load()) so here it
            # should be a nested collection and IDs should be ref to root collections.
            return LoadElementAs.ID_REF
        else:
            # Only collections at the root level of blendfile are id def, so here it can only be struct
            return LoadElementAs.STRUCT

    if isinstance(attr, T.Mesh):
        return LoadElementAs.ID_REF

    if same_rna(attr_property, T.PointerProperty):
        element_property = attr_property.fixed_type
    else:
        element_property = attr_property

    if is_ID_subclass_rna(element_property.bl_rna):
        # TODO this is wrong for scene master collection
        return LoadElementAs.ID_DEF

    return LoadElementAs.STRUCT


MAX_DEPTH = 30


# @debug_check_stack_overflow
def read_attribute(attr: Any, attr_property: T.Property, visit_state: VisitState):
    """
    Load a property into a python object of the appropriate type, be it a Proxy or a native python object
    """
    try:
        visit_state.recursion_guard.push(attr_property.name)

        attr_type = type(attr)

        if is_builtin(attr_type):
            return attr
        if is_vector(attr_type):
            return list(attr)
        if is_matrix(attr_type):
            return [list(col) for col in attr.col]

        # We have tested the types that are usefully reported by the python binding, now harder work.
        # These were implemented first and may be better implemented with the bl_rna property of the parent struct
        if attr_type == T.bpy_prop_array:
            return [e for e in attr]

        if attr_type == T.bpy_prop_collection:
            # need to know for each element if it is a ref or id
            load_as = load_as_what(attr_property, attr, visit_state.root_ids)
            assert load_as != LoadElementAs.ID_DEF
            if load_as == LoadElementAs.STRUCT:
                from mixer.blender_data.struct_collection_proxy import StructCollectionProxy

                return StructCollectionProxy.make(attr_property).load(attr, attr_property, visit_state)
            else:
                from mixer.blender_data.datablock_collection_proxy import DatablockCollectionProxy

                # LoadElementAs.ID_REF:
                # References into Blenddata collection, for instance D.scenes[0].objects
                return DatablockCollectionProxy().load_as_IDref(attr, visit_state)

        # TODO merge with previous case
        if isinstance(attr_property, T.CollectionProperty):
            from mixer.blender_data.struct_collection_proxy import StructCollectionProxy

            return StructCollectionProxy().load(attr, attr_property, visit_state)

        bl_rna = attr_property.bl_rna
        if bl_rna is None:
            logger.warning("Not implemented: attribute %s", attr)
            return None

        if issubclass(attr_type, T.PropertyGroup):
            from mixer.blender_data.struct_proxy import StructProxy

            return StructProxy().load(attr, visit_state)

        if issubclass(attr_type, T.ID):
            if attr.is_embedded_data:
                from mixer.blender_data.datablock_proxy import DatablockProxy

                return DatablockProxy.make(attr_property).load(attr, visit_state)
            else:
                from mixer.blender_data.datablock_ref_proxy import DatablockRefProxy

                return DatablockRefProxy().load(attr, visit_state)
        elif issubclass(attr_type, T.bpy_struct):
            from mixer.blender_data.struct_proxy import StructProxy

            return StructProxy().load(attr, visit_state)

        raise ValueError(f"Unsupported attribute type {attr_type} without bl_rna for attribute {attr} ")
    finally:
        visit_state.recursion_guard.pop()


def write_attribute(bl_instance, key: Union[str, int], value: Any, visit_state: VisitState):
    """
    Write a value into a Blender property
    """
    # Like in apply_attribute parent and key are needed to specify a L-value
    if bl_instance is None:
        logger.warning("unexpected write None attribute")
        return

    try:
        if isinstance(value, Proxy):
            value.save(bl_instance, key, visit_state)
        else:
            assert type(key) is str

            prop = bl_instance.bl_rna.properties.get(key)
            if prop is None:
                # Don't log this, too many messages
                # f"Attempt to write to non-existent attribute {bl_instance}.{key} : skipped"
                return

            if not prop.is_readonly:
                try:
                    setattr(bl_instance, key, value)
                except TypeError as e:
                    # common for enum that have unsupported default values, such as FFmpegSettings.ffmpeg_preset,
                    # which seems initialized at "" and triggers :
                    #   TypeError('bpy_struct: item.attr = val: enum "" not found in (\'BEST\', \'GOOD\', \'REALTIME\')')
                    logger.info(f"write attribute skipped {bl_instance}.{key}...")
                    logger.info(f" ...Exception: {repr(e)}")

    except TypeError:
        # common for enum that have unsupported default values, such as FFmpegSettings.ffmpeg_preset,
        # which seems initialized at "" and triggers :
        #   TypeError('bpy_struct: item.attr = val: enum "" not found in (\'BEST\', \'GOOD\', \'REALTIME\')')
        logger.warning(f"write attribute skipped {bl_instance}.{key}...")
        for line in traceback.format_exc().splitlines():
            logger.warning(f" ... {line}")
    except AttributeError as e:
        if isinstance(bl_instance, bpy.types.Collection) and bl_instance.name == "Master Collection" and key == "name":
            pass
        else:
            logger.warning(f"write attribute skipped {bl_instance}.{key}...")
            logger.warning(f" ...Exception: {repr(e)}")

    except Exception:
        logger.warning(f"write attribute skipped {bl_instance}.{key}...")
        for line in traceback.format_exc().splitlines():
            logger.warning(f" ... {line}")


def apply_attribute(parent, key: Union[str, int], proxy_value, delta: Delta, visit_state: VisitState, to_blender=True):
    """
    Applies a delta to the Blender attribute identified by bl_instance.key or bl_instance[key]

    Args:
        parent:
        key:
        proxy_value:
        delta:

    Returns: a value to store into the updated proxy
    """

    # Like in write_attribute parent and key are needed to specify a L-value
    # assert type(delta) == DeltaUpdate

    value = delta.value
    # assert proxy_value is None or type(proxy_value) == type(value)

    try:
        if isinstance(proxy_value, Proxy):
            return proxy_value.apply(parent, key, delta, visit_state, to_blender)

        if to_blender:
            try:
                # try is less costly than fetching the property to find if the attribute is readonly
                setattr(parent, key, value)
            except Exception as e:
                logger.warning(f"apply_attribute: setattr({parent}, {key}, {value})")
                logger.warning(f"... exception {e})")
        return value

    except Exception as e:
        logger.warning(f"diff exception for attr: {e}")
        raise


def diff_attribute(item: Any, item_property: T.Property, value: Any, visit_state: VisitState) -> Optional[Delta]:
    """
    Computes a difference between a blender item and a proxy value

    Args:
        item: the blender item
        item_property: the property of item as found in its enclosing object
        value: a proxy value

    """
    try:
        if isinstance(value, Proxy):
            return value.diff(item, item_property, visit_state)

        # An attribute mappable on a python builtin type
        # TODO overkill to call read_attribute because it is not a proxy type
        blender_value = read_attribute(item, item_property, visit_state)
        if blender_value != value:
            # TODO This is too coarse (whole lists)
            return DeltaUpdate(blender_value)

    except Exception as e:
        logger.warning(f"diff exception for attr: {e}")
        raise

    return None