"""Reading, editing and writing .docx packages as a dict of parts."""

from __future__ import annotations

import posixpath
import zipfile
from collections import OrderedDict
from pathlib import Path

from lxml import etree

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "ep": "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}

REL_IMAGE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
REL_HYPERLINK = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"

_PARSER = etree.XMLParser(remove_blank_text=False, huge_tree=True)


def q(tag: str) -> str:
    """Qualified name from a ``prefix:local`` string."""
    prefix, local = tag.split(":")
    return "{%s}%s" % (NS[prefix], local)


class Package:
    def __init__(self, parts: "OrderedDict[str, bytes]"):
        self.parts = parts
        self._xml: dict[str, etree._Element] = {}

    @classmethod
    def open(cls, path: Path) -> "Package":
        with zipfile.ZipFile(path) as z:
            parts = OrderedDict((n, z.read(n)) for n in z.namelist())
        return cls(parts)

    # -- xml access -----------------------------------------------------------
    def xml(self, name: str) -> etree._Element:
        if name not in self._xml:
            self._xml[name] = etree.fromstring(self.parts[name], _PARSER)
        return self._xml[name]

    def has(self, name: str) -> bool:
        return name in self.parts or name in self._xml

    def remove(self, name: str) -> None:
        self.parts.pop(name, None)
        self._xml.pop(name, None)

    def save(self, path: Path) -> None:
        for name, root in self._xml.items():
            self.parts[name] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        order = ["[Content_Types].xml"] + [n for n in self.parts if n != "[Content_Types].xml"]
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for n in order:
                z.writestr(n, self.parts[n])

    # -- relationships ----------------------------------------------------------
    def rels_name(self, part: str) -> str:
        d, f = part.rsplit("/", 1)
        return f"{d}/_rels/{f}.rels"

    def add_relationship(self, part: str, rel_type: str, target: str) -> str:
        rels = self.xml(self.rels_name(part))
        ids = {r.get("Id") for r in rels}
        n = 1
        while f"rId{n}" in ids:
            n += 1
        rid = f"rId{n}"
        el = etree.SubElement(rels, q("rel:Relationship"))
        el.set("Id", rid)
        el.set("Type", rel_type)
        el.set("Target", target)
        return rid

    def add_media(self, data: bytes, ext: str) -> str:
        ext = ext.lower().lstrip(".")
        n = 1
        while f"word/media/sn2docx_image{n}.{ext}" in self.parts:
            n += 1
        name = f"word/media/sn2docx_image{n}.{ext}"
        self.parts[name] = data
        self.ensure_default_content_type(ext, {"png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg", "gif": "image/gif"}[ext])
        return self.add_relationship("word/document.xml", REL_IMAGE, name.split("/", 1)[1])

    def ensure_default_content_type(self, ext: str, ctype: str) -> None:
        ct = self.xml("[Content_Types].xml")
        for d in ct.findall(q("ct:Default")):
            if d.get("Extension", "").lower() == ext:
                return
        el = etree.SubElement(ct, q("ct:Default"))
        el.set("Extension", ext)
        el.set("ContentType", ctype)

    def drop_orphan_media(self) -> None:
        """Remove media parts no relationship points to."""
        targets = set()
        for name in self.parts.keys() | self._xml.keys():
            if name.endswith(".rels"):
                for r in self.xml(name):
                    targets.add(resolve_target(name, r.get("Target", "")))
        for name in list(self.parts):
            if name.startswith("word/media/") and name not in targets:
                self.remove(name)
                ct = self.xml("[Content_Types].xml")
                for o in ct.findall(q("ct:Override")):
                    if o.get("PartName") == "/" + name:
                        ct.remove(o)


def resolve_target(rels_name: str, target: str) -> str:
    """Package part a relationship points to: word/_rels/document.xml.rels + media/x.png -> word/media/x.png."""
    if target.startswith("/"):
        return target.lstrip("/")
    folder = rels_name.split("_rels/")[0].rstrip("/")
    return posixpath.normpath(posixpath.join(folder, target))


def make_reference_doc(template: Path, dest: Path) -> Path:
    """Copy the Word template, keeping styles/header/footer/section but no content or media."""
    pkg = Package.open(template)
    doc = pkg.xml("word/document.xml")
    body = doc.find(q("w:body"))
    sect = body.find(q("w:sectPr"))
    for child in list(body):
        if child is not sect:
            body.remove(child)
    body.insert(0, etree.Element(q("w:p")))
    rels = pkg.xml("word/_rels/document.xml.rels")
    for r in list(rels):
        if r.get("Type") in (REL_IMAGE, REL_HYPERLINK):
            rels.remove(r)
    for name in list(pkg.parts):
        if name.startswith("word/media/"):
            pkg.remove(name)
    ct = pkg.xml("[Content_Types].xml")
    for o in list(ct.findall(q("ct:Override"))):
        if o.get("PartName", "").startswith("/word/media/"):
            ct.remove(o)
    pkg.save(dest)
    return dest


def text_of(el: etree._Element) -> str:
    return "".join(t.text or "" for t in el.iter(q("w:t")))

