import itertools
from operator import itemgetter
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .._typing import T_bbox, T_num, T_obj, T_obj_list
from .clustering import cluster_objects


def objects_to_rect(objects: Iterable[T_obj]) -> Dict[str, T_num]:
    """
    Given an iterable of objects, return the smallest rectangle (i.e. a
    dict with "x0", "top", "x1", and "bottom" keys) that contains them
    all.
    """
    return bbox_to_rect(objects_to_bbox(objects))


def objects_to_bbox(objects: Iterable[T_obj]) -> T_bbox:
    """
    Given an iterable of objects, return the smallest bounding box that
    contains them all.
    """
    return merge_bboxes(map(bbox_getter, objects))


bbox_getter = itemgetter("x0", "top", "x1", "bottom")


def obj_to_bbox(obj: T_obj) -> T_bbox:
    """
    Return the bounding box for an object.
    """
    bbox: T_bbox = bbox_getter(obj)
    return bbox


def bbox_to_rect(bbox: T_bbox) -> Dict[str, T_num]:
    """
    Return the rectangle (i.e a dict with keys "x0", "top", "x1",
    "bottom") for an object.
    """
    return {"x0": bbox[0], "top": bbox[1], "x1": bbox[2], "bottom": bbox[3]}


def merge_bboxes(bboxes: Iterable[T_bbox]) -> T_bbox:
    """
    Given an iterable of bounding boxes, return the smallest bounding box
    that contains them all.
    """
    x0, top, x1, bottom = zip(*bboxes)
    return (min(x0), min(top), max(x1), max(bottom))


def get_bbox_overlap(a: T_bbox, b: T_bbox, half_open: bool = False) -> Optional[T_bbox]:
    a_left, a_top, a_right, a_bottom = a
    b_left, b_top, b_right, b_bottom = b
    o_left = max(a_left, b_left)
    o_right = min(a_right, b_right)
    o_bottom = min(a_bottom, b_bottom)
    o_top = max(a_top, b_top)
    o_width = o_right - o_left
    o_height = o_bottom - o_top

    if half_open:
        # Treat bbox `b` as the half-open region [x0, x1) x [top, bottom).
        # Positive-area objects must overlap with positive area; zero-width
        # or zero-height objects (lines) must fall inside the region.
        if o_width < 0 or o_height < 0:
            return None
        if a_left == a_right:
            inside_x = b_left <= a_left < b_right
            if inside_x and o_bottom > o_top:
                return (a_left, o_top, a_left, o_bottom)
            return None
        if a_top == a_bottom:
            inside_y = b_top <= a_top < b_bottom
            if inside_y and o_right > o_left:
                return (o_left, a_top, o_right, a_top)
            return None
        if o_width > 0 and o_height > 0:
            return (o_left, o_top, o_right, o_bottom)
        return None
    elif o_height >= 0 and o_width >= 0 and o_height + o_width > 0:
        return (o_left, o_top, o_right, o_bottom)
    else:
        return None


def calculate_area(bbox: T_bbox) -> T_num:
    left, top, right, bottom = bbox
    if left > right or top > bottom:
        raise ValueError(f"{bbox} has a negative width or height.")
    return (right - left) * (bottom - top)


def clip_obj(
    obj: T_obj,
    bbox: T_bbox,
    half_open: bool = False,
    provenance: bool = False,
    transform: Optional[Any] = None,
    crop_bbox: Optional[T_bbox] = None,
    y_origin: Optional[T_num] = None,
) -> Optional[T_obj]:
    obj_bbox = obj_to_bbox(obj)
    source_bbox = tuple(obj.get("source_bbox", obj_bbox))

    # Selection always uses the object's source (unclipped) extent, so that
    # chained crops select the same objects as the equivalent single crop.
    selection = get_bbox_overlap(source_bbox, bbox, half_open=half_open)
    if selection is None:
        return None

    # The output geometry, however, is limited to the currently visible
    # extent, which may already have been clipped by a parent page.
    overlap = get_bbox_overlap(obj_bbox, bbox, half_open=half_open)
    if overlap is None:
        return None

    dims = bbox_to_rect(overlap)
    copy = dict(obj)

    for attr in ["x0", "top", "x1", "bottom"]:
        copy[attr] = dims[attr]

    diff = dims["top"] - obj["top"]
    if "doctop" in copy:
        copy["doctop"] = obj["doctop"] + diff
    if y_origin is not None:
        if "y0" in copy:
            copy["y0"] = y_origin - copy["bottom"]
        if "y1" in copy:
            copy["y1"] = y_origin - copy["top"]
    copy["width"] = copy["x1"] - copy["x0"]
    copy["height"] = copy["bottom"] - copy["top"]

    if provenance:
        copy = add_provenance(
            copy,
            source_bbox=source_bbox,
            visible_bbox=tuple(obj_to_bbox(copy)),
            transform=transform,
            crop_bbox=crop_bbox if crop_bbox is not None else bbox,
        )

    return copy


def add_provenance(
    obj: T_obj,
    source_bbox: T_bbox,
    visible_bbox: Optional[T_bbox] = None,
    transform: Optional[Any] = None,
    crop_bbox: Optional[T_bbox] = None,
    crop_bboxes: Optional[List[T_bbox]] = None,
) -> T_obj:
    """
    Attach coordinate provenance to an object.

    ``source_bbox`` is the object's unclipped bounding box in the root
    (original) page's coordinate space. ``visible_bbox`` is its (possibly
    clipped) bounding box in the current page's coordinate space.
    """
    copy = dict(obj)
    if visible_bbox is None:
        visible_bbox = tuple(obj_to_bbox(obj))  # type: ignore

    source_bbox = tuple(source_bbox)  # type: ignore
    visible_bbox = tuple(visible_bbox)  # type: ignore

    source_pdf_bbox: Optional[T_bbox] = None
    visible_pdf_bbox: Optional[T_bbox] = None
    if transform is not None:
        source_pdf_bbox = transform.to_pdf_bbox(source_bbox)
        visible_pdf_bbox = transform.to_pdf_bbox(visible_bbox)

    clipped = tuple(source_bbox) != tuple(visible_bbox)
    existing = obj.get("provenance") or {}
    crop_bboxes = list(
        crop_bboxes if crop_bboxes is not None else existing.get("crop_bboxes", [])
    )
    if crop_bbox is not None and crop_bbox not in crop_bboxes:
        crop_bboxes.append(crop_bbox)

    datum = {
        "source_bbox": source_bbox,
        "visible_bbox": visible_bbox,
        "source_pdf_bbox": source_pdf_bbox,
        "visible_pdf_bbox": visible_pdf_bbox,
        "original_bbox": source_bbox,
        "original_visible_bbox": visible_bbox,
        "original_pdf_bbox": source_pdf_bbox,
        "original_visible_pdf_bbox": visible_pdf_bbox,
        "clipped": clipped,
        "fully_visible": not clipped,
        "crop_bbox": crop_bbox,
        "crop_bboxes": crop_bboxes,
        "coordinate_space": "page",
        "pdf_coordinate_space": "pdf",
        "source_index": obj.get("source_index"),
        "object_type": obj.get("object_type"),
    }

    copy["provenance"] = datum
    copy["source_bbox"] = source_bbox
    copy["source_page_bbox"] = source_bbox
    copy["page_bbox"] = source_bbox
    copy["original_bbox"] = source_bbox
    copy["original_page_bbox"] = source_bbox
    copy["visible_bbox"] = visible_bbox
    copy["visible_page_bbox"] = visible_bbox
    copy["source_pdf_bbox"] = source_pdf_bbox
    copy["source_original_bbox"] = source_pdf_bbox
    copy["original_pdf_bbox"] = source_pdf_bbox
    copy["pdf_bbox"] = source_pdf_bbox
    copy["visible_pdf_bbox"] = visible_pdf_bbox
    copy["visible_original_bbox"] = visible_pdf_bbox
    copy["original_visible_bbox"] = visible_bbox
    copy["original_visible_pdf_bbox"] = visible_pdf_bbox
    copy["clipped"] = clipped
    copy["fully_visible"] = not clipped
    copy["crop_bbox"] = crop_bbox
    copy["crop_bboxes"] = crop_bboxes

    if transform is not None:
        copy["cropbox"] = transform.cropbox
        copy["cropbox_pdf"] = transform.cropbox_pdf
        copy["source_cropbox"] = transform.cropbox
        copy["source_cropbox_pdf"] = transform.cropbox_pdf
        copy["rotation"] = transform.rotation
        copy["mediabox"] = transform.page_mediabox
        copy["mediabox_pdf"] = transform.mediabox
        copy["mediabox_relative"] = transform.mediabox_relative

    for name, value in (
        ("source_x0", source_bbox[0]),
        ("source_top", source_bbox[1]),
        ("source_x1", source_bbox[2]),
        ("source_bottom", source_bbox[3]),
    ):
        copy[name] = value

    for name, value in (
        ("visible_x0", visible_bbox[0]),
        ("visible_top", visible_bbox[1]),
        ("visible_x1", visible_bbox[2]),
        ("visible_bottom", visible_bbox[3]),
    ):
        copy[name] = value

    if source_pdf_bbox is not None:
        for name, value in (
            ("original_x0", source_pdf_bbox[0]),
            ("original_y0", source_pdf_bbox[1]),
            ("original_x1", source_pdf_bbox[2]),
            ("original_y1", source_pdf_bbox[3]),
            ("pdf_x0", source_pdf_bbox[0]),
            ("pdf_y0", source_pdf_bbox[1]),
            ("pdf_x1", source_pdf_bbox[2]),
            ("pdf_y1", source_pdf_bbox[3]),
        ):
            copy[name] = value

    if visible_pdf_bbox is not None:
        for name, value in (
            ("visible_original_x0", visible_pdf_bbox[0]),
            ("visible_original_y0", visible_pdf_bbox[1]),
            ("visible_original_x1", visible_pdf_bbox[2]),
            ("visible_original_y1", visible_pdf_bbox[3]),
            ("visible_pdf_x0", visible_pdf_bbox[0]),
            ("visible_pdf_y0", visible_pdf_bbox[1]),
            ("visible_pdf_x1", visible_pdf_bbox[2]),
            ("visible_pdf_y1", visible_pdf_bbox[3]),
        ):
            copy[name] = value

    return copy


def merge_provenance(objs: Iterable[T_obj]) -> Dict[str, Any]:
    """
    Merge provenance from a group of objects (e.g., the chars of a word).

    Returns an empty dict when none of the objects carry provenance.
    """
    objs = [o for o in objs if "source_bbox" in o]
    if not objs:
        return {}

    source_bbox = merge_bboxes(o["source_bbox"] for o in objs)
    visible_bbox = merge_bboxes(o["visible_bbox"] for o in objs)
    source_pdf = [o["source_pdf_bbox"] for o in objs if o.get("source_pdf_bbox")]
    visible_pdf = [o["visible_pdf_bbox"] for o in objs if o.get("visible_pdf_bbox")]
    source_pdf_bbox = merge_bboxes(source_pdf) if source_pdf else None
    visible_pdf_bbox = merge_bboxes(visible_pdf) if visible_pdf else None

    crop_bboxes: List[T_bbox] = []
    for obj in objs:
        for bbox in obj.get("crop_bboxes", []):
            if bbox not in crop_bboxes:
                crop_bboxes.append(bbox)

    first = objs[0]
    clipped = tuple(source_bbox) != tuple(visible_bbox)
    merged: Dict[str, Any] = {
        "source_bbox": source_bbox,
        "visible_bbox": visible_bbox,
        "source_pdf_bbox": source_pdf_bbox,
        "visible_pdf_bbox": visible_pdf_bbox,
        "original_bbox": source_bbox,
        "original_visible_bbox": visible_bbox,
        "original_pdf_bbox": source_pdf_bbox,
        "original_visible_pdf_bbox": visible_pdf_bbox,
        "clipped": clipped,
        "fully_visible": not clipped,
        "crop_bbox": crop_bboxes[-1] if crop_bboxes else None,
        "crop_bboxes": crop_bboxes,
        "coordinate_space": "page",
        "pdf_coordinate_space": "pdf",
        "object_type": first.get("object_type"),
    }
    for key in (
        "cropbox",
        "cropbox_pdf",
        "source_cropbox",
        "source_cropbox_pdf",
        "rotation",
        "mediabox",
        "mediabox_pdf",
        "mediabox_relative",
    ):
        if key in first:
            merged[key] = first[key]

    def add_axes(prefix: str, bbox: Optional[T_bbox], names: Tuple[str, ...]) -> None:
        if bbox is None:
            return
        for name, value in zip(names, bbox):
            merged[f"{prefix}_{name}"] = value

    add_axes("source", source_bbox, ("x0", "top", "x1", "bottom"))
    add_axes("visible", visible_bbox, ("x0", "top", "x1", "bottom"))
    add_axes("original", source_bbox, ("x0", "top", "x1", "bottom"))
    add_axes("visible_original", visible_bbox, ("x0", "top", "x1", "bottom"))
    add_axes("original_pdf", source_pdf_bbox, ("x0", "y0", "x1", "y1"))
    add_axes("original_visible_pdf", visible_pdf_bbox, ("x0", "y0", "x1", "y1"))
    add_axes("pdf", source_pdf_bbox, ("x0", "y0", "x1", "y1"))
    add_axes("visible_pdf", visible_pdf_bbox, ("x0", "y0", "x1", "y1"))
    return merged


def apply_provenance(obj: T_obj, provenance: Dict[str, Any]) -> T_obj:
    """Apply a merged provenance dict to an object."""
    if not provenance:
        return obj
    copy = dict(obj)
    copy["provenance"] = dict(provenance)
    copy["source_bbox"] = provenance["source_bbox"]
    copy["source_page_bbox"] = provenance["source_bbox"]
    copy["page_bbox"] = provenance["source_bbox"]
    copy["original_bbox"] = provenance["source_bbox"]
    copy["source_original_bbox"] = provenance["source_pdf_bbox"]
    copy["original_page_bbox"] = provenance["source_bbox"]
    copy["source_pdf_bbox"] = provenance["source_pdf_bbox"]
    copy["original_pdf_bbox"] = provenance["source_pdf_bbox"]
    copy["visible_bbox"] = provenance["visible_bbox"]
    copy["visible_page_bbox"] = provenance["visible_bbox"]
    copy["visible_pdf_bbox"] = provenance["visible_pdf_bbox"]
    copy["visible_original_bbox"] = provenance["visible_pdf_bbox"]
    copy["original_visible_bbox"] = provenance["visible_bbox"]
    copy["original_visible_pdf_bbox"] = provenance["visible_pdf_bbox"]
    copy["pdf_bbox"] = provenance["source_pdf_bbox"]
    copy["clipped"] = provenance["clipped"]
    copy["fully_visible"] = provenance["fully_visible"]
    copy["crop_bbox"] = provenance["crop_bbox"]
    copy["crop_bboxes"] = provenance["crop_bboxes"]
    for key, value in provenance.items():
        if key in (
            "cropbox",
            "cropbox_pdf",
            "source_cropbox",
            "source_cropbox_pdf",
            "rotation",
            "mediabox",
            "mediabox_pdf",
            "mediabox_relative",
        ):
            copy[key] = value
        elif key.endswith("_bbox") or key.endswith("_bboxes"):
            copy[key] = value
        elif key.startswith(("source_", "visible_", "original_", "pdf_")):
            copy[key] = value
    return copy


def annotate_obj(
    obj: T_obj,
    transform: Optional[Any] = None,
    crop_bbox: Optional[T_bbox] = None,
    crop_bboxes: Optional[List[T_bbox]] = None,
) -> T_obj:
    """Attach provenance to an unclipped object, using its current bbox."""
    if "x0" not in obj or "top" not in obj or "x1" not in obj or "bottom" not in obj:
        return obj
    source_bbox = obj.get("source_bbox", obj_to_bbox(obj))
    return add_provenance(
        obj,
        source_bbox=source_bbox,
        visible_bbox=tuple(obj_to_bbox(obj)),
        transform=transform,
        crop_bbox=crop_bbox,
        crop_bboxes=crop_bboxes,
    )


def intersects_bbox(
    objs: Iterable[T_obj], bbox: T_bbox, half_open: bool = False
) -> T_obj_list:
    """
    Filters objs to only those intersecting the bbox
    """
    return [
        obj
        for obj in objs
        if get_bbox_overlap(obj_to_bbox(obj), bbox, half_open=half_open) is not None
    ]


def within_bbox(
    objs: Iterable[T_obj],
    bbox: T_bbox,
    half_open: bool = False,
    provenance: bool = False,
    transform: Optional[Any] = None,
    crop_bbox: Optional[T_bbox] = None,
    y_origin: Optional[T_num] = None,
) -> T_obj_list:
    """
    Filters objs to only those fully within the bbox

    With ``half_open=True``, the bbox is treated as the half-open region
    [x0, x1) x [top, bottom); an object that merely touches an excluded
    edge is not considered to be within the region.
    """
    if half_open:
        results = []
        for obj in objs:
            obj_bbox = obj.get("source_bbox", obj_to_bbox(obj))
            ox0, otop, ox1, obottom = obj_bbox
            bx0, btop, bx1, bbottom = bbox
            if ox1 < ox0 or obottom < otop:
                continue
            if ox1 > ox0:
                within_x = bx0 <= ox0 and ox1 <= bx1
            else:
                within_x = bx0 <= ox0 < bx1
            if obottom > otop:
                within_y = btop <= otop and obottom <= bbottom
            else:
                within_y = btop <= otop < bbottom
            if within_x and within_y:
                results.append(obj)
    else:
        results = [
            obj
            for obj in objs
            if get_bbox_overlap(obj.get("source_bbox", obj_to_bbox(obj)), bbox)
            == obj.get("source_bbox", obj_to_bbox(obj))
        ]

    if provenance:
        results = [
            add_provenance(
                obj,
                source_bbox=obj.get("source_bbox", obj_to_bbox(obj)),
                visible_bbox=tuple(obj_to_bbox(obj)),
                transform=transform,
                crop_bbox=crop_bbox if crop_bbox is not None else bbox,
            )
            for obj in results
        ]
    return results


def outside_bbox(
    objs: Iterable[T_obj],
    bbox: T_bbox,
    half_open: bool = False,
    provenance: bool = False,
    transform: Optional[Any] = None,
    crop_bbox: Optional[T_bbox] = None,
) -> T_obj_list:
    """
    Filters objs to only those fully outside the bbox
    """
    results = [
        obj
        for obj in objs
        if get_bbox_overlap(
            obj.get("source_bbox", obj_to_bbox(obj)), bbox, half_open=half_open
        )
        is None
    ]
    if provenance:
        results = [
            add_provenance(
                obj,
                source_bbox=obj.get("source_bbox", obj_to_bbox(obj)),
                visible_bbox=tuple(obj_to_bbox(obj)),
                transform=transform,
                crop_bbox=crop_bbox if crop_bbox is not None else bbox,
            )
            for obj in results
        ]
    return results


def crop_to_bbox(
    objs: Iterable[T_obj],
    bbox: T_bbox,
    half_open: bool = False,
    provenance: bool = False,
    transform: Optional[Any] = None,
    y_origin: Optional[T_num] = None,
    crop_bbox: Optional[T_bbox] = None,
) -> T_obj_list:
    """
    Filters objs to only those intersecting the bbox,
    and crops the extent of the objects to the bbox.

    When ``provenance=True``, every output object is annotated with both its
    source (unclipped) bbox and its visible bbox.
    """
    results: T_obj_list = []
    for obj in objs:
        source_bbox = tuple(obj.get("source_bbox", obj_to_bbox(obj)))

        clipped = clip_obj(
            obj,
            bbox,
            half_open=half_open,
            provenance=False,
            y_origin=y_origin,
        )
        if clipped is None:
            continue

        if provenance:
            clipped = add_provenance(
                clipped,
                source_bbox=source_bbox,
                visible_bbox=tuple(obj_to_bbox(clipped)),
                transform=transform,
                crop_bbox=crop_bbox if crop_bbox is not None else bbox,
            )
        results.append(clipped)
    return results


def move_object(obj: T_obj, axis: str, value: T_num) -> T_obj:
    assert axis in ("h", "v")
    if axis == "h":
        new_items = [
            ("x0", obj["x0"] + value),
            ("x1", obj["x1"] + value),
        ]
    if axis == "v":
        new_items = [
            ("top", obj["top"] + value),
            ("bottom", obj["bottom"] + value),
        ]
        if "doctop" in obj:
            new_items += [("doctop", obj["doctop"] + value)]
        if "y0" in obj:
            new_items += [
                ("y0", obj["y0"] - value),
                ("y1", obj["y1"] - value),
            ]
    return obj.__class__(tuple(obj.items()) + tuple(new_items))


def snap_objects(objs: Iterable[T_obj], attr: str, tolerance: T_num) -> T_obj_list:
    axis = {"x0": "h", "x1": "h", "top": "v", "bottom": "v"}[attr]
    list_objs = list(objs)
    clusters = cluster_objects(list_objs, itemgetter(attr), tolerance)
    avgs = [sum(map(itemgetter(attr), cluster)) / len(cluster) for cluster in clusters]
    snapped_clusters = [
        [move_object(obj, axis, avg - obj[attr]) for obj in cluster]
        for cluster, avg in zip(clusters, avgs)
    ]
    return list(itertools.chain(*snapped_clusters))


def resize_object(obj: T_obj, key: str, value: T_num) -> T_obj:
    assert key in ("x0", "x1", "top", "bottom")
    old_value = obj[key]
    diff = value - old_value
    new_items = [
        (key, value),
    ]
    if key == "x0":
        assert value <= obj["x1"]
        new_items.append(("width", obj["x1"] - value))
    elif key == "x1":
        assert value >= obj["x0"]
        new_items.append(("width", value - obj["x0"]))
    elif key == "top":
        assert value <= obj["bottom"]
        new_items.append(("doctop", obj["doctop"] + diff))
        new_items.append(("height", obj["height"] - diff))
        if "y1" in obj:
            new_items.append(("y1", obj["y1"] - diff))
    elif key == "bottom":
        assert value >= obj["top"]
        new_items.append(("height", obj["height"] + diff))
        if "y0" in obj:
            new_items.append(("y0", obj["y0"] - diff))
    return obj.__class__(tuple(obj.items()) + tuple(new_items))


def curve_to_edges(curve: T_obj) -> T_obj_list:
    point_pairs = zip(curve["pts"], curve["pts"][1:])
    return [
        {
            "object_type": "curve_edge",
            "x0": min(p0[0], p1[0]),
            "x1": max(p0[0], p1[0]),
            "top": min(p0[1], p1[1]),
            "doctop": min(p0[1], p1[1]) + (curve["doctop"] - curve["top"]),
            "bottom": max(p0[1], p1[1]),
            "width": abs(p0[0] - p1[0]),
            "height": abs(p0[1] - p1[1]),
            "orientation": "v" if p0[0] == p1[0] else ("h" if p0[1] == p1[1] else None),
        }
        for p0, p1 in point_pairs
    ]


def rect_to_edges(rect: T_obj) -> T_obj_list:
    top, bottom, left, right = [dict(rect) for x in range(4)]
    top.update(
        {
            "object_type": "rect_edge",
            "height": 0,
            "y0": rect["y1"],
            "bottom": rect["top"],
            "orientation": "h",
        }
    )
    bottom.update(
        {
            "object_type": "rect_edge",
            "height": 0,
            "y1": rect["y0"],
            "top": rect["top"] + rect["height"],
            "doctop": rect["doctop"] + rect["height"],
            "orientation": "h",
        }
    )
    left.update(
        {
            "object_type": "rect_edge",
            "width": 0,
            "x1": rect["x0"],
            "orientation": "v",
        }
    )
    right.update(
        {
            "object_type": "rect_edge",
            "width": 0,
            "x0": rect["x1"],
            "orientation": "v",
        }
    )
    return [top, bottom, left, right]


def line_to_edge(line: T_obj) -> T_obj:
    edge = dict(line)
    edge["orientation"] = "h" if (line["top"] == line["bottom"]) else "v"
    return edge


def obj_to_edges(obj: T_obj) -> T_obj_list:
    t = obj["object_type"]
    if "_edge" in t:
        return [obj]
    elif t == "line":
        return [line_to_edge(obj)]
    else:
        return {"rect": rect_to_edges, "curve": curve_to_edges}[t](obj)


def filter_edges(
    edges: Iterable[T_obj],
    orientation: Optional[str] = None,
    edge_type: Optional[str] = None,
    min_length: T_num = 1,
) -> T_obj_list:
    if orientation not in ("v", "h", None):
        raise ValueError("Orientation must be 'v' or 'h'")

    def test(e: T_obj) -> bool:
        dim = "height" if e["orientation"] == "v" else "width"
        et_correct = e["object_type"] == edge_type if edge_type is not None else True
        orient_correct = orientation is None or e["orientation"] == orientation
        return bool(et_correct and orient_correct and (e[dim] >= min_length))

    return list(filter(test, edges))
