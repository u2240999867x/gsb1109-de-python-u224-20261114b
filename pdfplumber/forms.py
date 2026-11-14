"""
Separate collection of AcroForm field-tree values and per-page widget
appearances.

A single logical field may be represented by several widget annotations,
e.g. one widget on each page. The field dictionary's ``/V`` entry holds the
logical value, while each widget's ``/AS`` entry only describes that widget's
on-page appearance. The two are therefore collected independently:

* a widget showing ``/Off`` does not mean the field itself is unfilled, and
* a missing appearance on one page must never erase a known logical value.
"""

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

from pdfminer.pdftypes import PDFObjRef
from pdfminer.psparser import PSLiteral

from .utils.pdfinternals import decode_text, resolve

if TYPE_CHECKING:  # pragma: no cover
    from .pdf import PDF

logger = logging.getLogger(__name__)

OFF_STATE = "Off"

# /Ff bits for /Btn fields (PDF 1.7 spec, table 221)
BTN_RADIO_FLAG = 1 << 15  # 32768: radio button group
BTN_PUSHBUTTON_FLAG = 1 << 16  # 65536: push button, holds no value

STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_INHERITED = "inherited"
STATUS_DUPLICATE_NAME = "duplicate_name"
STATUS_DUPLICATE_PATH = "duplicate_path"
STATUS_MISSING_PARENT = "missing_parent"
STATUS_ORPHAN_WIDGET = "orphan_widget"
STATUS_CONFLICT = "conflict"
STATUS_APPEARANCE_MISSING = "appearance_missing"

MAX_PARENT_DEPTH = 100


def _literal_name(value: Any) -> Optional[str]:
    if isinstance(value, PSLiteral):
        return value.name
    return None


def _decode_name(value: Any) -> Optional[str]:
    """Decode a /T partial name, which may be bytes or text."""
    if value is None:
        return None
    if isinstance(value, PSLiteral):
        return value.name
    if isinstance(value, bytes):
        return decode_text(value)
    if isinstance(value, str):
        return value
    return str(value)


def _normalize_value(value: Any) -> Any:
    """Convert PDF primitives into JSON-friendly Python values."""
    if isinstance(value, PDFObjRef):
        return {"object_ref": value.objid}
    if isinstance(value, PSLiteral):
        return value.name
    if isinstance(value, bytes):
        return decode_text(value)
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    return value


def _is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value == "":
        return True
    if isinstance(value, list) and len(value) == 0:
        return True
    # /Off is the unchecked state, i.e. effectively no value.
    if value == OFF_STATE:
        return True
    return False


class PDFForm:
    """
    Snapshot of an AcroForm.

    Attributes:
        fields: one record per terminal field-tree node (logical values)
        widgets: one record per widget annotation (per-page appearances)
        field_report: one row per detected anomaly. Each row carries the page
            number and object references needed to trace the problem back
            into the source PDF.
    """

    def __init__(self, pdf: "PDF") -> None:
        self.pdf = pdf
        self._fields: Optional[List[Dict[str, Any]]] = None
        self._widgets: Optional[List[Dict[str, Any]]] = None
        self._field_report: Optional[List[Dict[str, Any]]] = None
        self._nodes: Dict[int, Dict[str, Any]] = {}
        self._terminal_refs: Set[int] = set()
        self._field_record_by_ref: Dict[int, Dict[str, Any]] = {}

    @staticmethod
    def _resolve_ref(ref: Any) -> Tuple[Optional[int], Any]:
        """Return (object number, resolved dictionary).

        A dangling reference resolves to ``None`` rather than raising, and an
        inline object is reported with object number ``None``.
        """
        if isinstance(ref, PDFObjRef):
            return ref.objid, resolve(ref)
        return None, ref

    def _collect_field_tree(self) -> None:
        catalog = getattr(self.pdf.doc, "catalog", None)
        acroform = resolve(catalog.get("AcroForm")) if catalog else None
        if not isinstance(acroform, dict):
            return
        fields = acroform.get("Fields")
        if not isinstance(fields, (list, tuple)):
            return

        visited: Set[int] = set()

        def walk(ref: Any, parent_path: str, parent_ref: Optional[int]) -> None:
            objid, node = self._resolve_ref(ref)
            if objid is not None:
                if objid in visited:
                    return
                visited.add(objid)
            if not isinstance(node, dict):
                if objid is not None:
                    logger.warning(
                        "AcroForm field object %s could not be resolved; skipped.",
                        objid,
                    )
                return

            local_name = _decode_name(node.get("T"))
            if local_name is None:
                path = parent_path
            elif parent_path:
                path = f"{parent_path}.{local_name}"
            else:
                path = local_name

            kids = node.get("Kids")
            has_kids = isinstance(kids, (list, tuple)) and len(kids) > 0
            record: Dict[str, Any] = {
                "object_ref": objid,
                "parent_ref": parent_ref,
                "local_name": local_name,
                "path": path,
                "has_kids": has_kids,
                "node": node,
                "ancestors": [],
            }
            if objid is not None:
                self._nodes[objid] = record

            if has_kids:
                for child in kids:
                    child_ref = (
                        child.objid if isinstance(child, PDFObjRef) else None
                    )
                    walk(child, path, objid if objid is not None else child_ref)

        for top_ref in fields:
            walk(top_ref, "", None)

    def _field_type(self, record: Dict[str, Any]) -> Optional[str]:
        node = record["node"]
        ft = _literal_name(node.get("FT"))
        if ft is not None:
            return ft
        for ancestor_ref in record["ancestors"]:
            ancestor = self._nodes.get(ancestor_ref)
            if ancestor is not None:
                ft = _literal_name(ancestor["node"].get("FT"))
                if ft is not None:
                    return ft
        return None

    def _resolve_field_value(
        self, record: Dict[str, Any]
    ) -> Tuple[Any, str, Optional[int]]:
        """
        Resolve the logical /V value, walking ancestors when the terminal node
        does not define one itself.

        Returns (normalized value, source, object_ref_where_found).
        Source is one of: own, inherited, empty.
        """
        node = record["node"]
        if "V" in node:
            return _normalize_value(node["V"]), "own", record["object_ref"]
        for ancestor_ref in record["ancestors"]:
            ancestor = self._nodes.get(ancestor_ref)
            if ancestor is not None and "V" in ancestor["node"]:
                return (
                    _normalize_value(ancestor["node"]["V"]),
                    "inherited",
                    ancestor_ref,
                )
        return None, "empty", None

    def _is_radio(self, field_type: Optional[str], node: Dict[str, Any]) -> bool:
        if field_type != "Btn":
            return False
        flags = node.get("Ff")
        if not isinstance(flags, int):
            return False
        if flags & BTN_PUSHBUTTON_FLAG:
            return False
        return bool(flags & BTN_RADIO_FLAG)

    @staticmethod
    def _is_pure_widget(node: Dict[str, Any]) -> bool:
        """A pure widget annotation carries no field dictionary of its own.

        A merged field-and-widget dictionary defines ``/FT`` itself and is
        therefore also treated as a (terminal) field node.
        """
        return _literal_name(node.get("Subtype")) == "Widget" and "FT" not in node

    def _build_field_records(self) -> List[Dict[str, Any]]:
        def is_terminal(rec: Dict[str, Any]) -> bool:
            node = rec["node"]
            if self._is_pure_widget(node):
                return False
            kids = node.get("Kids")
            if not isinstance(kids, (list, tuple)) or len(kids) == 0:
                return True
            # A field whose Kids are all widget annotations is itself the
            # terminal field; a node with field-type children is not.
            for kid in kids:
                kid_node = resolve(kid) if isinstance(kid, PDFObjRef) else kid
                if isinstance(kid_node, dict) and not self._is_pure_widget(
                    kid_node
                ):
                    return False
            return True

        terminal = [rec for rec in self._nodes.values() if is_terminal(rec)]
        self._terminal_refs = {
            rec["object_ref"] for rec in terminal if rec["object_ref"] is not None
        }

        name_groups: Dict[str, List[Dict[str, Any]]] = {}
        path_groups: Dict[str, List[Dict[str, Any]]] = {}
        records: List[Dict[str, Any]] = []

        for rec in terminal:
            node = rec["node"]
            field_type = self._field_type(rec)
            value, value_source, value_ref = self._resolve_field_value(rec)
            flags = node.get("Ff") if isinstance(node.get("Ff"), int) else 0
            is_pushbutton = field_type == "Btn" and bool(
                flags & BTN_PUSHBUTTON_FLAG
            )
            record = {
                "object_type": "field",
                "path": rec["path"],
                "local_name": rec["local_name"],
                "object_ref": rec["object_ref"],
                "parent_ref": rec["parent_ref"],
                "field_type": field_type,
                "is_radio": self._is_radio(field_type, node),
                "value": value,
                "value_source": value_source,
                "value_defined_at": value_ref,
                "is_pushbutton": is_pushbutton,
                "widget_refs": [],
                "duplicate_name": False,
                "duplicate_path": False,
            }
            records.append(record)
            if rec["local_name"] is not None:
                name_groups.setdefault(rec["local_name"], []).append(record)
            if rec["path"]:
                path_groups.setdefault(rec["path"], []).append(record)

        for group in name_groups.values():
            if len(group) > 1:
                for record in group:
                    record["duplicate_name"] = True
        for group in path_groups.values():
            if len(group) > 1:
                for record in group:
                    record["duplicate_path"] = True
        return records

    def _widget_parent_chain(
        self, widget_node: Dict[str, Any]
    ) -> Tuple[List[int], Optional[int]]:
        """Walk /Parent refs.

        Returns (resolved ancestor object ids, first unresolvable object id).
        """
        chain: List[int] = []
        seen: Set[int] = set()
        unresolved: Optional[int] = None
        parent = widget_node.get("Parent")
        depth = 0
        while isinstance(parent, PDFObjRef) and depth < MAX_PARENT_DEPTH:
            objid = parent.objid
            if objid in seen:
                break
            seen.add(objid)
            parent_node = resolve(parent)
            if not isinstance(parent_node, dict):
                unresolved = objid
                break
            chain.append(objid)
            parent = parent_node.get("Parent")
            depth += 1
        return chain, unresolved

    def _terminal_for_chain(
        self, chain: List[int]
    ) -> Optional[Dict[str, Any]]:
        for objid in chain:
            if objid in self._terminal_refs:
                return self._field_record_by_ref.get(objid)
        return None

    def _appearance(
        self, widget_node: Dict[str, Any], chain: List[int]
    ) -> Tuple[Optional[str], str]:
        if "AS" in widget_node:
            return _literal_name(widget_node["AS"]), "own"
        for objid in chain:
            node_record = self._nodes.get(objid)
            if node_record is not None and "AS" in node_record["node"]:
                return _literal_name(node_record["node"]["AS"]), "inherited"
        return None, "missing"

    @staticmethod
    def _normalize_rect(rect: Any) -> Optional[List[Any]]:
        rect = resolve(rect)
        if isinstance(rect, (list, tuple)):
            return [resolve(item) for item in rect]
        return None

    @staticmethod
    def _widget_on_states(widget_node: Dict[str, Any]) -> Optional[Set[str]]:
        """Selectable (non-/Off) states offered by a widget's /AP /N dict.

        Returns ``None`` when the widget carries no appearance dictionary,
        i.e. when its capabilities cannot be determined.
        """
        appearance_dict = resolve(widget_node.get("AP"))
        if not isinstance(appearance_dict, dict):
            return None
        normal_states = resolve(appearance_dict.get("N"))
        if not isinstance(normal_states, dict):
            return None
        on_states: Set[str] = set()
        for key in normal_states:
            name = key.name if isinstance(key, PSLiteral) else key
            if isinstance(name, str) and name != OFF_STATE:
                on_states.add(name)
        return on_states

    @staticmethod
    def _check_appearance(
        field_type: Optional[str],
        is_radio: bool,
        logical_value: Any,
        appearance: Optional[str],
        appearance_source: str,
        on_states: Optional[Set[str]],
    ) -> str:
        # Text and signature fields do not use /AS, so a missing appearance
        # is normal for them.
        if field_type not in ("Btn", "Ch"):
            return STATUS_OK
        logical_empty = _is_empty_value(logical_value)
        if appearance_source == "missing":
            # A widget with no /AS at all is only suspicious when the field
            # holds a value that this widget is able to display.
            if logical_empty:
                return STATUS_OK
            if on_states is not None and logical_value not in on_states:
                return STATUS_OK
            return STATUS_APPEARANCE_MISSING
        if logical_empty:
            # An appearance is shown but the field has no logical value.
            return STATUS_CONFLICT if appearance != OFF_STATE else STATUS_OK
        if appearance == OFF_STATE:
            # In a radio group, a widget that cannot display the selected
            # value is expected to remain /Off; only a capable widget staying
            # /Off means this page's appearance is missing.
            if (
                is_radio
                and on_states is not None
                and logical_value not in on_states
            ):
                return STATUS_OK
            return STATUS_APPEARANCE_MISSING
        if appearance != logical_value:
            return STATUS_CONFLICT
        return STATUS_OK

    def _field_type_from_chain(self, chain: List[int]) -> Optional[str]:
        for objid in chain:
            node_record = self._nodes.get(objid)
            if node_record is not None:
                ft = _literal_name(node_record["node"].get("FT"))
                if ft is not None:
                    return ft
        return None

    def _collect_widgets(self) -> List[Dict[str, Any]]:
        widgets: List[Dict[str, Any]] = []
        for page in self.pdf.pages:
            raw_annots = resolve(page.page_obj.annots)
            if not isinstance(raw_annots, (list, tuple)):
                continue
            for annot_ref in raw_annots:
                objid, annot = self._resolve_ref(annot_ref)
                if not isinstance(annot, dict):
                    continue
                subtype = _literal_name(annot.get("Subtype"))
                if (
                    subtype != "Widget"
                    and "FT" not in annot
                    and "Parent" not in annot
                ):
                    continue

                chain, unresolved_parent = self._widget_parent_chain(annot)
                immediate_parent = annot.get("Parent")
                parent_ref = (
                    immediate_parent.objid
                    if isinstance(immediate_parent, PDFObjRef)
                    else None
                )
                field_record = self._terminal_for_chain(chain)
                # A merged field-and-widget annotation is itself the terminal
                # field and therefore has no /Parent chain to walk.
                if field_record is None and objid in self._field_record_by_ref:
                    field_record = self._field_record_by_ref[objid]

                appearance, appearance_source = self._appearance(annot, chain)
                on_states = self._widget_on_states(annot)
                if field_record is not None:
                    field_path = field_record["path"]
                    field_ref = field_record["object_ref"]
                    local_name = field_record["local_name"]
                    field_type = field_record["field_type"]
                    is_radio = field_record["is_radio"]
                    logical_value = field_record["value"]
                    value_source = field_record["value_source"]
                else:
                    field_path = None
                    field_ref = None
                    local_name = _decode_name(annot.get("T"))
                    field_type = _literal_name(annot.get("FT"))
                    if field_type is None:
                        field_type = self._field_type_from_chain(chain)
                    is_radio = False
                    logical_value = None
                    value_source = "missing"

                if unresolved_parent is not None:
                    status = STATUS_MISSING_PARENT
                elif field_record is None:
                    status = STATUS_ORPHAN_WIDGET
                else:
                    status = self._check_appearance(
                        field_type,
                        is_radio,
                        logical_value,
                        appearance,
                        appearance_source,
                        on_states,
                    )

                widget = {
                    "object_type": "widget",
                    "object_ref": objid,
                    "page_number": page.page_number,
                    "parent_ref": parent_ref,
                    "field_ref": field_ref,
                    "field_path": field_path,
                    "local_name": local_name,
                    "field_type": field_type,
                    "is_radio": is_radio,
                    "logical_value": logical_value,
                    "value_source": value_source,
                    "appearance_state": appearance,
                    "appearance_source": appearance_source,
                    "on_states": (
                        sorted(on_states) if on_states is not None else None
                    ),
                    "status": status,
                    "rect": self._normalize_rect(annot.get("Rect")),
                    "data": annot,
                }
                widgets.append(widget)
                if field_record is not None and objid is not None:
                    field_record["widget_refs"].append(objid)
        return widgets

    def _build_report(
        self, fields: List[Dict[str, Any]], widgets: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        report: List[Dict[str, Any]] = []

        for field in fields:
            base = {
                "page_number": None,
                "field_path": field["path"],
                "local_name": field["local_name"],
                "field_ref": field["object_ref"],
                "object_ref": field["object_ref"],
                "parent_ref": field["parent_ref"],
            }
            if field["duplicate_name"]:
                report.append(
                    {
                        **base,
                        "status": STATUS_DUPLICATE_NAME,
                        "value": field["value"],
                        "appearance_state": None,
                        "details": (
                            "Partial field name is used by more than one field; "
                            "the full parent path disambiguates them."
                        ),
                    }
                )
            if field["duplicate_path"]:
                report.append(
                    {
                        **base,
                        "status": STATUS_DUPLICATE_PATH,
                        "value": field["value"],
                        "appearance_state": None,
                        "details": "More than one field shares the same full path.",
                    }
                )
            if field["is_pushbutton"]:
                continue
            if _is_empty_value(field["value"]):
                if (
                    field["value_source"] == "inherited"
                    and field["value_defined_at"] is not None
                ):
                    empty_details = (
                        "Inherited /V from object "
                        f"{field['value_defined_at']} is empty."
                    )
                else:
                    empty_details = (
                        "No logical /V value found on the field or any of "
                        "its ancestors."
                    )
                report.append(
                    {
                        **base,
                        "status": STATUS_EMPTY,
                        "value": None,
                        "appearance_state": None,
                        "details": empty_details,
                    }
                )
            elif field["value_source"] == "inherited":
                report.append(
                    {
                        **base,
                        "status": STATUS_INHERITED,
                        "value": field["value"],
                        "appearance_state": None,
                        "details": (
                            "Field has no /V of its own; value inherited from "
                            f"object {field['value_defined_at']}."
                        ),
                    }
                )

        widget_details = {
            STATUS_APPEARANCE_MISSING: (
                "Widget is unchecked or has no appearance while the field has "
                "a logical value; only this page's appearance is missing."
            ),
            STATUS_CONFLICT: (
                "Widget appearance state does not match the field's logical "
                "value."
            ),
            STATUS_MISSING_PARENT: (
                "Widget /Parent reference could not be resolved to a "
                "field-tree node."
            ),
            STATUS_ORPHAN_WIDGET: (
                "Widget's parent chain is not reachable from AcroForm /Fields."
            ),
        }
        for widget in widgets:
            if widget["status"] == STATUS_OK:
                continue
            report.append(
                {
                    "status": widget["status"],
                    "page_number": widget["page_number"],
                    "field_path": widget["field_path"],
                    "local_name": widget["local_name"],
                    "field_ref": widget["field_ref"],
                    "object_ref": widget["object_ref"],
                    "parent_ref": widget["parent_ref"],
                    "value": widget["logical_value"],
                    "appearance_state": widget["appearance_state"],
                    "details": widget_details.get(widget["status"], ""),
                }
            )
        return report

    def _build(self) -> None:
        self._collect_field_tree()
        # Ancestor chains can only be completed once the full node index
        # exists.
        for record in self._nodes.values():
            chain: List[int] = []
            parent_ref = record["parent_ref"]
            seen: Set[int] = set()
            while parent_ref is not None and parent_ref in self._nodes:
                if parent_ref in seen:
                    break
                seen.add(parent_ref)
                chain.append(parent_ref)
                parent_ref = self._nodes[parent_ref]["parent_ref"]
            record["ancestors"] = chain

        fields = self._build_field_records()
        self._field_record_by_ref = {
            record["object_ref"]: record
            for record in fields
            if record["object_ref"] is not None
        }
        widgets = self._collect_widgets()
        self._fields = fields
        self._widgets = widgets
        self._field_report = self._build_report(fields, widgets)

    @property
    def fields(self) -> List[Dict[str, Any]]:
        if self._fields is None:
            self._build()
        assert self._fields is not None
        return self._fields

    @property
    def widgets(self) -> List[Dict[str, Any]]:
        if self._widgets is None:
            self._build()
        assert self._widgets is not None
        return self._widgets

    @property
    def field_report(self) -> List[Dict[str, Any]]:
        if self._field_report is None:
            self._build()
        assert self._field_report is not None
        return self._field_report
