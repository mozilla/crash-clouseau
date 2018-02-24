# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import json
import re
from .logger import logger
import unicodedata
from validate_email import validate_email


PATS = [
    (re.compile(r'^([\w\t ’\'\.\-]+)\[?<+([^@>]+@[^>]+)>?$', re.UNICODE), [2, 1, 0]),  # foo bar <...@...>
    (re.compile(r'^\"([\w\t ’\'\.\-]+)\"[\t ]*\[?<+([^@>]+@[^>]+)>?$', re.UNICODE), [2, 1, 0]),  # "foo bar" <...@...>
    (re.compile(r'^<([^@>]+@[^>]+)>?$', re.UNICODE), [1, 0, 0]),  # <...@...>
    (re.compile(r'^([\w\t ’\'\.\-]+)[\[\(]:?([^\)\]]+)[\]\)][\"\t ]*[\(<]([^@>]+@[^>]+)[\)>]?$', re.UNICODE), [3, 1, 2]),  # foo bar (:toto) <...@...>
    (re.compile(r'^([\w\t ’\'\.\-]+)\(([^@\)>]+@[^\)>]+)[\)>]?$', re.UNICODE), [2, 1, 0]),  # foo bar (...@...)
    (re.compile(r'^([^@\t ]+@[^\t ]+)$', re.UNICODE), [1, 0, 0]),  # ...@...
    (re.compile(r'^([\w\t ’\'\.\-]+)<([\w\t \.\+\-]+)>$', re.UNICODE), [0, 1, 2]),  # foo bar <toto>
    (re.compile(r'^<([\w\t ’\'\.\+]+)>$', re.UNICODE), [0, 0, 1]),  # <toto>
    (re.compile(r'^<([\w\t ’\'\.\+]+)>[\t ]*([^@\t ]+@[^\t ]+)$', re.UNICODE), [2, 1, 0]),  # <toto> ...@...
    (re.compile(r'^([\w\t ’\'\.\-]+)$', re.UNICODE), [0, 1, 0]),  # foo bar
    (re.compile(r'^((?:[\w’\'\.\-]+[\t ]+)+)([^@\t >]+@[^\t >]+)>?$', re.UNICODE), [2, 1, 0]),  # foo bar toto@titi
    (re.compile(r'^([\w\t ’\'\.\-]+)[\[\(]:?([^\)]+)[\]\)]$', re.UNICODE), [0, 1, 2]),  # foo bar (:toto)
    (re.compile(r'^([\w\t ’\'\.\-]+):([\w_]+)$', re.UNICODE), [0, 1, 2]),  # foo bar :toto
    (re.compile(r'^([\w\t ’\'\.\-]+):([^\t ]+)[\t ]*[\(<]([^@>]+@[^>]+)[\)>]?$', re.UNICODE), [3, 1, 2]),  # foo bar :toto <...@...>
    (re.compile(r'^([^\t @]+@[^\t ]+)[\t ]*<([^@>]+@[^>]+)>?$', re.UNICODE), [2, 0, 0]),  # ...@... <...@...>
    (re.compile(r'^([^\t @]+@[^\t ]+)[\t ]*<([\w\t \.\+]+)>?$', re.UNICODE), [1, 0, 2]),  # ...@... <toto>
    (re.compile(r'^[\[\(]:?([^\)]+)[\]\)][\"\t ]*[\(<]([^@>]+@[^>]+)[\)>]?$', re.UNICODE), [2, 0, 1]),  # (:toto) <...@...>
    (re.compile(r'^([\w\t ’\'\.\-\\]+)<([^@>]+@[^>]+)>?$', re.UNICODE), [2, 1, 0]),  # foo \"bar\" <...@...>
    (re.compile(r'^([\w’\'\.\-\+]+)[\t ]*<([^@>]+@[^>]+)>?$', re.UNICODE), [2, 1, 0]),  # foo-bar.toto <...@...>
    (re.compile(r'^([\w\t ’\'\.\-]+)\[?<+([^@>]+@[^>]+)>[\t ]*[\w\t \(\)]+$', re.UNICODE), [2, 1, 0]),  # foo bar <...@...> (tutu)
    (re.compile(r'^([\w\t ’\'\.\-]+)[\[\(]:?([^\)\]]+)[\]\)][\t ]*[\[\(]:?([^\)\]]+)[\]\)][\t ]*[\"\t ]*[\(<]([^@>]+@[^>]+)[\)>]?$', re.UNICODE), [4, 1, 2]),  # foo bar (:toto) (:titi) <...@...>
]

SPLIT_PAT = re.compile(r',|;|(?: \+ )|(?: and )|/|&')
NON_LETTER = re.compile(r'[^a-zA-Z]')
SPECIAL_PAT_1 = re.compile(r'^(\w+)[\t ]*,[\t ]*(\w+)[\t ]*<([^@]+@[^>]+)>?$', re.UNICODE)
SPECIAL_PAT_2 = re.compile(r'[\t ]*,[\t ]*')
SPECIAL_PAT_3 = (re.compile(r'^([\w\t ’\'\.\-]+)<([^@]+@[^>]+)>.*$', re.UNICODE), [2, 1, 0])
SPECIAL_PAT_4 = re.compile(r'^\"?([\w’\'\.\-\+]+)[\t ]*[\(\"]([^\)\"]+)[\)\"][\t ]*([\w’\'\"\.\-\+]+)\"?[\t ]*<([^@>]+@[^>]+)>?$', re.UNICODE)  # foo (bar) toto <...@...>
SPECIAL_PAT_5 = re.compile(r'^([^>]+)[\t ]*<+([^@>]+@[^>]+)>?$', re.UNICODE)  # ... <...@...>
BUG_PAT = re.compile(r'bug[0-9]+', re.I)
ENCODINGS = ['iso-8859-1', 'iso-8859-2']


def clean_author(author):
    """Remove typos we can have in a author field"""
    if author.startswith('\"') and author.endswith('\"'):
        author = author[1:-1]
    if author.endswith('.'):
        author = author[:-1]
    if author.endswith('>>'):
        author = author[:-1]
    if author.startswith('='):
        author = author[1:]
    author = author.strip()
    author = author.replace('%40', '@')
    author = author.replace('%gmail', '@gmail')
    author = unicodedata.normalize('NFC', author)

    return author


def check_pat(pat, positions, author):
    """Check a pattern and return a triple (email, real, nick) according to positions"""
    m = pat.match(author)
    if m:
        return tuple(m.group(p).strip() if p != 0 else '' for p in positions)
    return None


def check_common(author):
    """Check for common patterns (as found in PATS)"""
    # we check each regex in PATS
    for pat, positions in PATS:
        r = check_pat(pat, positions, author)
        if r:
            return r

    r = special4(author)
    if r:
        return r

    return None


def check_multiple(author):
    """Check for multiple authors we can have in one author field"""
    authors = SPLIT_PAT.split(author)
    if len(authors) >= 2 and all('@' in a for a in authors):
        res = []
        fail = []
        for author in authors:
            author = author.strip()
            r = check_common(author)
            if r:
                if isinstance(r, list):
                    res += r
                else:
                    res.append(r)
            else:
                fail.append(author)
        return res, fail
    return None, None


def to_ascii_form(s):
    """Remove accent and non-acii chars"""
    return unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode('utf-8')


def rm_non_letter(s):
    """Remove non-letter chars"""
    return NON_LETTER.sub('', s)


def rm_accents(s):
    """Remove accent and punctuation chars"""
    r = ''
    for c in unicodedata.normalize('NFD', s):
        cat = unicodedata.category(c)[0]
        # if cat is not Mark, Punctuation
        if cat not in {'M', 'P'}:
            r += c
    return r


def cmp_name_email(name, email_name):
    """Compare name and first part of email address"""
    name = to_ascii_form(name.lower())
    email = rm_non_letter(email_name.lower())

    if ' ' in name:
        toks = list(map(rm_non_letter, name.split(' ')))
        if ''.join(toks) == email:
            return True
        for tok in toks:
            if len(tok) >= 5 and tok in email:
                return True
        if len(toks) == 2:
            toks = toks[::-1]
            if ''.join(toks) == email:
                return True
            if toks[0][0] + toks[1] == email:
                return True
            if toks[1][0] + toks[0] == email:
                return True
    elif name in email:
        return True
    return False


def special1(author):
    # we've something like foo, bar <foo.bar@toto>
    m = SPECIAL_PAT_1.match(author)
    if m:
        foo, bar, email = m.group(1).lower(), m.group(2).lower(), m.group(3).lower()
        if foo in email:
            return (email, foo + ' ' + bar, '')
    return None


def special2(author):
    # we've something like toto foo, titi bar <toto@...>
    toks = author.split(' <')
    if len(toks) == 2:
        email = toks[1][:-1] if toks[1].endswith('>') else toks[1]
        if '@' in email:
            before_at = email.split('@')[0]
            authors = SPECIAL_PAT_2.split(toks[0])
            for a in authors:
                if cmp_name_email(a, before_at):
                    return (email, a, '')
    return None


def special3(author):
    # we've something like foo, bar <foo.bar@toto>
    pat, positions = SPECIAL_PAT_3
    r = check_pat(pat, positions, author)
    if r:
        return r
    return None


def special4(author):
    m = SPECIAL_PAT_4.match(author)
    if m:
        return (m.group(4), m.group(1) + ' ' + m.group(3), m.group(2))
    return None


def special5(author):
    m = SPECIAL_PAT_5.match(author)
    if m:
        return (m.group(2), m.group(1), '')
    return None


def post_process(r, author):
    """Post process the triple (email, real, nick) to remove typos or stuff like that"""
    email, real, nick = r
    email = email.strip(' .,')
    real = real.strip()
    nick = nick.strip()
    if email.startswith('mailto:'):
        email = email[7:]
    elif email.startswith('h<'):
        email = email[2:]
    email = email.replace(',', '.').replace('@.', '@').replace('.@', '@').replace(' ', '')
    email = BUG_PAT.sub('', email)

    if email and not validate_email(email):
        logger.error('Email not valid for: {}'.format(author))

    return (email, real, nick)


def analyze_author_helper(author, first=False):
    """Analyze an author to try to guess the triple (email, realname, nickname)"""
    r = check_common(author)
    if r:
        return r

    r, fail = check_multiple(author)
    if fail:
        logger.error('Failed to parse authors: {}'.format(author))
    if r:
        return r

    for special in [special1, special2, special3]:
        r = special(author)
        if r:
            return r

    if first:
        return None

    # maybe we've an encoding issue...
    for enc in ENCODINGS:
        try:
            a = bytes(author, enc).decode('utf-8')
            r = analyze_author_helper(a, first=True)
            if r:
                return r
            break
        except Exception:
            pass

    r = special5(author)
    if r:
        return r

    logger.error('Failed to parse author: {}'.format(author))

    return None


def analyze_author(author, clean=True):
    """Analyze an author to try to guess the triple (email, realname, nickname)"""
    if clean:
        author = clean_author(author)

    r = analyze_author_helper(author)
    if r:
        if not isinstance(r, list):
            return [post_process(r, author)]
        else:
            return [post_process(x, author) for x in r]

    return []


def cmp_name_email1(n, e):
    """Compare name and email to try to find a correspondance between them"""
    if e and len(n) == 2:
        L = list(filter(None, map(to_ascii_form, n)))
        if len(L) == 2:
            if e.startswith('.'.join(L)) or e.startswith('.'.join(L[::-1])):
                return True
            if e.startswith(L[0][0] + L[1]) or e.startswith(L[1][0] + L[0]):
                return True
    return False


def equal_author(a, b):
    """Try to guess if two authors are the same"""
    ea, ra, na = a
    eb, rb, nb = b
    if ea and eb:
        if ea == eb:
            # same email
            return True

    if (na and eb and eb.startswith(na)) or (nb and ea and ea.startswith(nb)):
        return True

    if ra and rb:
        if ra == rb:
            # same real name
            return True

        names_a = set(map(lambda s: rm_accents(s.lower()), ra.split(' ')))
        names_b = set(map(lambda s: rm_accents(s.lower()), rb.split(' ')))
        if names_a == names_b:
            return True

        if len(names_a & names_b) >= 2:
            return True

        cmp_name_email1(names_a, eb)
        cmp_name_email1(names_b, ea)

    return False


def gather(authors):
    """Try to gather the same authors in the same bucket"""
    res = []
    for author in authors:
        added = False
        for r in res:
            if equal_author(r[0], author):
                r.append(author)
                added = True
                break
        if not added:
            res.append([author])
    return res


def collect_authors(path, data=None):
    """Collect authors from a pushlog"""
    if not data:
        with open(path, 'r') as In:
            data = json.load(In)

    toanalyze = set()
    for i in data:
        author = i['author']
        if author not in toanalyze:
            toanalyze.add(clean_author(author))

    res = set(x for author in toanalyze for x in analyze_author(author, clean=False))
    return res


def stats(path):
    """Make some stats on authors"""
    with open(path, 'r') as In:
        data = json.load(In)

    res = {}
    for i in data:
        author = i['author']
        if author not in res:
            res[author] = {'count': 1,
                           'author': analyze_author(author)}
        else:
            res[author]['count'] += 1

    return res
