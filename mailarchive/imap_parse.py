"""Analyse des réponses IMAP brutes (renvoyées par imaplib).

imaplib ne décode pas les structures IMAP : il renvoie des lignes d'octets et,
pour les « littéraux » ({123}), des tuples (entête, contenu). Ce module les
transforme en listes Python imbriquées, puis interprète BODYSTRUCTURE,
ENVELOPE et LIST.
"""
from __future__ import annotations

import base64
import re
from email.header import decode_header, make_header
from email.utils import collapse_rfc2231_value, decode_params, unquote

__all__ = [
    "Literal",
    "chunks_from_imaplib",
    "parse",
    "parse_fetch",
    "parse_list",
    "decode_imap_utf7",
    "decode_words",
    "iter_leaf_parts",
    "leaf_info",
    "envelope_sender",
    "envelope_subject",
]


class Literal(bytes):
    """Contenu d'un littéral IMAP ({n}) : toujours une chaîne, jamais un atome."""


_LIT_RE = re.compile(rb"\{(\d+)\+?\}\s*$")


def chunks_from_imaplib(data) -> list:
    out: list = []
    for item in data or []:
        if item is None:
            continue
        if isinstance(item, tuple):
            head = item[0]
            m = _LIT_RE.search(head)
            if m:
                head = head[: m.start()]
            out.append(bytes(head))
            out.append(Literal(item[1]))
            for extra in item[2:]:
                out.append(bytes(extra))
        else:
            out.append(bytes(item))
    return out


_DELIMS = b" \t\r\n()\""


def _tokenize(chunks):
    for ch in chunks:
        if isinstance(ch, Literal):
            yield "L", bytes(ch)
            continue
        i, n = 0, len(ch)
        while i < n:
            c = ch[i]
            if c in b" \t\r\n":
                i += 1
            elif c == 0x28:  # (
                yield "(", None
                i += 1
            elif c == 0x29:  # )
                yield ")", None
                i += 1
            elif c == 0x22:  # "
                i += 1
                buf = bytearray()
                while i < n and ch[i] != 0x22:
                    if ch[i] == 0x5C and i + 1 < n:  # backslash
                        i += 1
                    buf.append(ch[i])
                    i += 1
                i += 1
                yield "S", bytes(buf)
            else:
                j = i
                while j < n and ch[j] not in _DELIMS:
                    j += 1
                yield "A", ch[i:j]
                i = j


def _text(b: bytes) -> str:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("latin-1")


def parse(chunks, raw_literals: bool = False) -> list:
    """raw_literals=True : les littéraux restent en octets (contenu de mails, fichiers)."""
    stack: list[list] = [[]]
    for kind, val in _tokenize(chunks):
        if kind == "(":
            stack.append([])
        elif kind == ")":
            if len(stack) > 1:
                lst = stack.pop()
                stack[-1].append(lst)
        elif kind == "L":
            stack[-1].append(val if raw_literals else _text(val))
        elif kind == "S":
            stack[-1].append(_text(val))
        else:
            stack[-1].append(None if val.upper() == b"NIL" else _text(val))
    while len(stack) > 1:  # parenthèses non fermées : on tolère
        lst = stack.pop()
        stack[-1].append(lst)
    return stack[0]


def parse_fetch(data, raw_literals: bool = False) -> list[dict]:
    """Réponse de UID FETCH -> liste de dict {'UID': ..., 'BODYSTRUCTURE': ...}."""
    items = parse(chunks_from_imaplib(data), raw_literals)
    res: list[dict] = []
    i = 0
    while i < len(items):
        cur = items[i]
        if isinstance(cur, str) and cur.isdigit() and i + 1 < len(items) and isinstance(items[i + 1], list):
            lst = items[i + 1]
            d: dict = {}
            for k in range(0, len(lst) - 1, 2):
                if isinstance(lst[k], str):
                    d[lst[k].upper()] = lst[k + 1]
            res.append(d)
            i += 2
        else:
            i += 1
    return res


def parse_list(data) -> list[tuple[list[str], str | None, str]]:
    """Réponse de LIST -> [(drapeaux, délimiteur, nom brut)]."""
    items = parse(chunks_from_imaplib(data))
    out = []
    i = 0
    while i < len(items):
        if isinstance(items[i], list) and i + 2 < len(items):
            flags = [f for f in items[i] if isinstance(f, str)]
            delim, name = items[i + 1], items[i + 2]
            if isinstance(name, str):
                out.append((flags, delim, name))
            i += 3
        else:
            i += 1
    return out


def decode_imap_utf7(s: str) -> str:
    """Noms de dossiers IMAP (UTF-7 modifié, RFC 3501) -> texte lisible."""
    out = []
    i = 0
    while i < len(s):
        if s[i] == "&":
            j = s.find("-", i)
            if j == -1:
                out.append(s[i:])
                break
            if j == i + 1:
                out.append("&")
            else:
                b64 = s[i + 1 : j].replace(",", "/")
                b64 += "=" * (-len(b64) % 4)
                try:
                    out.append(base64.b64decode(b64).decode("utf-16-be"))
                except Exception:
                    out.append(s[i : j + 1])
            i = j + 1
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def decode_words(s: str | None) -> str:
    """Décode les mots encodés RFC 2047 (=?utf-8?B?...?=)."""
    if not s:
        return ""
    try:
        return str(make_header(decode_header(s)))
    except Exception:
        return s


def _pairs(lst) -> list[tuple[str, str]]:
    if not isinstance(lst, list):
        return []
    out = []
    for k in range(0, len(lst) - 1, 2):
        key, val = lst[k], lst[k + 1]
        if isinstance(key, str) and isinstance(val, str):
            out.append((key.lower(), val))
    return out


def _param(pairs: list[tuple[str, str]], wanted: str) -> str | None:
    """Valeur d'un paramètre MIME, en gérant RFC 2231 (filename*=utf-8''...)."""
    if not pairs:
        return None
    try:
        decoded = decode_params([("x", "")] + pairs)[1:]
    except Exception:
        decoded = pairs
    for key, val in decoded:
        if key.lower() != wanted:
            continue
        if isinstance(val, tuple):  # RFC 2231 : (jeu de caractères, langue, valeur %-décodée)
            charset, lang, raw = val
            return collapse_rfc2231_value((charset or "utf-8", lang, unquote(raw)), fallback_charset="utf-8")
        return decode_words(unquote(val))
    return None


def iter_leaf_parts(bs, path: str = "", parent: str = ""):
    """Parcourt un BODYSTRUCTURE et renvoie (chemin, partie, sous-type du parent)."""
    if not isinstance(bs, list) or not bs:
        return
    if isinstance(bs[0], list):  # multipart
        children = []
        subtype = ""
        for el in bs:
            if isinstance(el, list):
                children.append(el)
            else:
                subtype = (el or "").lower() if isinstance(el, str) else ""
                break
        for idx, child in enumerate(children, 1):
            yield from iter_leaf_parts(child, f"{path}.{idx}" if path else str(idx), subtype)
        return
    yield (path or "1"), bs, parent


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _looks_like_disposition(v) -> bool:
    return (
        isinstance(v, list)
        and len(v) >= 1
        and isinstance(v[0], str)
        and v[0].lower() in ("attachment", "inline")
    )


def leaf_info(p: list) -> dict:
    """Champs utiles d'une partie simple d'un BODYSTRUCTURE."""
    typ = (p[0] or "").lower() if len(p) > 0 and isinstance(p[0], str) else ""
    sub = (p[1] or "").lower() if len(p) > 1 and isinstance(p[1], str) else ""
    params = _pairs(p[2] if len(p) > 2 else None)
    cid = p[3] if len(p) > 3 and isinstance(p[3], str) else None
    enc = (p[5] or "").lower() if len(p) > 5 and isinstance(p[5], str) else ""
    size = _int(p[6] if len(p) > 6 else 0)

    if typ == "text":
        md5_idx = 8
    elif typ == "message" and sub in ("rfc822", "global"):
        md5_idx = 10
    else:
        md5_idx = 7

    disp, dparams = None, []
    cand = p[md5_idx + 1] if len(p) > md5_idx + 1 else None
    if not _looks_like_disposition(cand):
        cand = next((x for x in p[7:] if _looks_like_disposition(x)), None)
    if cand is not None:
        disp = cand[0].lower()
        dparams = _pairs(cand[1] if len(cand) > 1 else None)

    filename = _param(dparams, "filename") or _param(params, "name") or ""
    return {
        "type": typ,
        "subtype": sub,
        "mime": f"{typ}/{sub}",
        "encoding": enc,
        "size": size,
        "cid": cid,
        "disposition": disp,
        "filename": filename.strip(),
    }


def envelope_sender(env) -> tuple[str, str]:
    """(nom, adresse) de l'expéditeur depuis ENVELOPE."""
    try:
        addr = env[2][0]
        name = decode_words(addr[0]) if addr[0] else ""
        mailbox = addr[2] or ""
        host = addr[3] or ""
        return name, (f"{mailbox}@{host}" if host else mailbox).lower()
    except (TypeError, IndexError):
        return "", ""


def envelope_subject(env) -> str:
    try:
        return decode_words(env[1])
    except (TypeError, IndexError):
        return ""
