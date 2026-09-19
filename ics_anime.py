#!/usr/bin/env python3
"""
ics_anime.py - Builds an ICS calendar of current- or next-season anime episodes on the
streaming services you choose, with an interactive show picker.

A Python port of ics_anime.ps1. Every function and flow of the PowerShell
script is reproduced: the AniList client with backoff, LiveChart merging,
lineup-page attribution, prequel-walking synopsis resolution, the version 4
JSON database, the arrow-key menus, and the RFC 5545 exporter with merging.

The database format is compatible with ics_anime.ps1, so the two can share
anime-ics-db.json and the calendars they write merge into each other.

Provider toggles filter cached shows immediately. Removing a provider asks
separately whether to remove its shows from the ICS; shared shows stay selected
through any remaining enabled provider. Selection history suppresses repeat
review entries. The season picker offers current, next and future seasons,
fetching an uncached future season only when selected. Older provider-filtered
caches are refreshed once to recover their missing provider data.

Menu navigation repaints changed rows on compatible terminals and redraws the
screen for paging, resizing and returning from another screen.

Episode airtimes, titles and synopses come from AniList. Streaming attribution
comes from AniList external links, LiveChart, and optional per-season lineup
pages. All times are written to the calendar in UTC; calendar applications
convert them to the subscriber's local timezone.

AniList publishes Japanese broadcast airtimes. Streaming releases may happen
later and availability differs by region.

An upcoming season is mostly titles that have been announced but not scheduled.
Those are listed too: a show whose premiere is known only to the month gets an
all-day placeholder on the 1st, marked "(date TBA)", which moves to the real
date - incrementing SEQUENCE so subscribers follow it - as soon as one is
published.

Shows are NOT added to the calendar by default. A newly discovered title is
listed under "Shows not added" in the menu, and only the titles you add with
Space are exported. Running with -NoMenu on a fresh database therefore produces
an empty calendar until you have chosen some shows.

Requires Python 3.8+ and nothing outside the standard library.
"""

from __future__ import annotations

import argparse
import csv
import glob as globmodule
import html
import json
import os
import re
import shutil
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import urlparse

UTC = timezone.utc
NEWLINE = '\r\n' if os.name == 'nt' else '\n'
CRLF = '\r\n'
# Kept so calendars written by the PowerShell script still de-duplicate.
UID_SUFFIX = '@anilist-calendar.local'
DB_VERSION = 4
CALENDAR_FILENAME = 'anime-calendar.ics'
USER_AGENT = 'anime-ics/1.0'
ZWJ = '‍'
VARIATION_SELECTORS = ('︎', '️')

# Provider aliases normalize common AniList/LiveChart names. The database grows
# its provider directory dynamically from streaming links returned by both sites.
PROVIDER_CATALOG = [
    ('Crunchyroll', re.compile(r'crunchyroll', re.I)),
    ('HIDIVE', re.compile(r'hidive', re.I)),
    ('Prime Video', re.compile(r'(prime\s*video|primevideo|amazon\.)', re.I)),
    ('Netflix', re.compile(r'netflix', re.I)),
    ('Disney+', re.compile(r'(disney\s*\+|disneyplus|star\+)', re.I)),
]

GENRE_ITEMS = [
    ('All anime', 'All'),
    ('Fantasy, isekai and reincarnation', 'FantasyIsekaiReincarnation'),
    ('Non-fantasy / non-isekai / non-reincarnation', 'ExcludeFantasyIsekaiReincarnation'),
]


# ---------------------------------------------------------------------------
# Script-scope state
#
# The PowerShell script keeps these in $script: scope and mutates them from the
# menus (Refresh-ProviderSeasons rewrites $Providers, the genre menu rewrites
# $CategoryFilter). A single namespace keeps that shape without globals sprayed
# through every function.
# ---------------------------------------------------------------------------
S = SimpleNamespace(
    db=None,
    db_path='',
    db_dirty=False,
    anilist_requests=0,
    download_bytes={},
    args=None,
    providers=[],
    bound=set(),
    category_filter='All',
)


def add_download_bytes(source, count):
    """Add-DownloadBytes."""
    key = str(source or 'unknown')
    count = int(count)
    S.download_bytes[key] = S.download_bytes.get(key, 0) + count
    if S.db is not None:
        by_source = S.db.setdefault('ScrapeBytesBySource', {})
        by_source[key] = int(by_source.get(key, 0)) + count
        S.db['ScrapeBytesTotal'] = int(S.db.get('ScrapeBytesTotal', 0)) + count
        set_database_dirty()


def format_data_size(count):
    """Format a byte tally for compact menu display."""
    return '{0:.2f} MB'.format(int(count or 0) / (1024 ** 2))


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def year_type(value):
    number = int(value)
    if number < 2000 or number > 2100:
        raise argparse.ArgumentTypeError('Year must be between 2000 and 2100.')
    return number


def split_list(values):
    """-Providers Crunchyroll,'Prime Video' and -Providers A B both work."""
    out = []
    for value in values or []:
        for part in str(value).split(','):
            part = part.strip()
            if part:
                out.append(part)
    return out


def build_parser():
    parser = argparse.ArgumentParser(
        prog='ics_anime.py',
        allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='Build an ICS calendar of current- or next-season anime episodes.',
        epilog=(
            'Examples:\n'
            '  ics_anime.py                                    open the interactive picker\n'
            '  ics_anime.py -NextSeason -NoMenu                rebuild next season with no prompts\n'
            '  ics_anime.py -Providers Crunchyroll,"Prime Video" -CombineProviders -Refresh\n'
        ),
    )

    def add(names, **kwargs):
        parser.add_argument(*names, **kwargs)

    add(['-OutputPath', '--output-path'], dest='OutputPath', metavar='PATH',
        help='Destination .ics file, or a folder to write anime-calendar.ics into.')
    add(['-Providers', '--providers'], dest='Providers', nargs='+', metavar='NAME',
        help='Streaming services to include in non-interactive runs.')
    add(['-Season', '--season'], dest='Season', choices=['WINTER', 'SPRING', 'SUMMER', 'FALL'],
        type=str.upper, help='WINTER, SPRING, SUMMER or FALL. Defaults to the current season.')
    add(['-Year', '--year'], dest='Year', type=year_type, metavar='YYYY',
        help='Four-digit year (2000-2100). Defaults to the current year.')
    add(['-NextSeason', '--next-season'], dest='NextSeason', action='store_true',
        help='Target the season immediately after the current one.')
    add(['-IncludePastEpisodes', '--include-past-episodes'], dest='IncludePastEpisodes',
        action='store_true', help='Keep episodes that have already aired.')
    add(['-CombineProviders', '--combine-providers'], dest='CombineProviders', action='store_true',
        help='One event per episode listing every service.')
    add(['-CategoryFilter', '--category-filter'], dest='CategoryFilter', default='All',
        choices=['All', 'FantasyIsekaiReincarnation', 'ExcludeFantasyIsekaiReincarnation'],
        help='Controls only what the picker displays; it never adds anything.')
    add(['-ProviderOverridesPath', '--provider-overrides-path'], dest='ProviderOverridesPath',
        metavar='CSV', help='CSV with AniListId,Provider,Url columns for regional corrections.')
    add(['-LineupSourcePath', '--lineup-source-path'], dest='LineupSourcePath', metavar='CSV',
        help='CSV with Season,Year,Provider,Url columns pointing at seasonal lineup pages.')
    add(['-MergeFrom', '--merge-from'], dest='MergeFrom', nargs='+', metavar='PATH',
        help='Additional .ics files or wildcards to fold into the output.')
    add(['-Refresh', '--refresh'], dest='Refresh', action='store_true',
        help='Re-scrape the target season even when cached.')
    add(['-ListProviders', '--list-providers'], dest='ListProviders', action='store_true',
        help='Print providers discovered from cached links, then exit.')
    add(['-ListCache', '--list-cache'], dest='ListCache', action='store_true',
        help='Print database location, size and cached seasons, then exit.')
    add(['-ClearCache', '--clear-cache'], dest='ClearCache', action='store_true',
        help='Drop cached seasons and shows.')
    add(['-Force', '--force'], dest='Force', action='store_true',
        help='With -ClearCache, clear saved selections too.')
    add(['-AllDiscoveredProviders', '--all-discovered-providers'], dest='AllDiscoveredProviders',
        action='store_true', help='Enable every discovered provider instead of the saved selection.')
    add(['-MetricsPath', '--metrics-path'], dest='MetricsPath', metavar='PATH',
        help='Write a JSON summary of request counts and bytes downloaded.')
    add(['-DatabasePath', '--database-path'], dest='DatabasePath', metavar='PATH',
        help='Override the cache location. Defaults to anime-ics-db.json beside the script.')
    add(['-NoMenu', '--no-menu'], dest='NoMenu', action='store_true',
        help='Run non-interactively using the supplied parameters.')
    add(['-SkipStartupUpdateCheck', '--skip-startup-update-check'], dest='SkipStartupUpdateCheck',
        action='store_true', help='Skip the startup re-scrape for next-season changes.')
    add(['-Verbose', '--verbose'], dest='Verbose', action='store_true',
        help='Print the diagnostics the PowerShell script writes with Write-Verbose.')
    add(['-NoColor', '--no-color'], dest='NoColor', action='store_true',
        help='Disable ANSI colour output.')
    return parser


PARAMETER_NAMES = (
    'OutputPath', 'Providers', 'Season', 'Year', 'NextSeason', 'IncludePastEpisodes',
    'CombineProviders', 'CategoryFilter', 'ProviderOverridesPath', 'LineupSourcePath',
    'MergeFrom', 'Refresh', 'ListProviders', 'ListCache', 'ClearCache', 'Force',
    'AllDiscoveredProviders', 'MetricsPath', 'DatabasePath', 'NoMenu',
    'SkipStartupUpdateCheck', 'Verbose', 'NoColor',
)


def bound_parameters(argv):
    """The equivalent of $PSBoundParameters: which options were actually typed.

    -NextSeason must not be combined with an explicit -Season/-Year, and the
    season picker is skipped only when the command line already pinned one down,
    so 'supplied' has to be distinguishable from 'left at its default'.
    """
    lookup = {name.lower(): name for name in PARAMETER_NAMES}
    for name in PARAMETER_NAMES:
        lookup['-'.join(part.lower() for part in re.findall(r'[A-Z][a-z0-9]*', name))] = name
    bound = set()
    for token in argv:
        if not token.startswith('-') or token == '-':
            continue
        key = token.lstrip('-').split('=', 1)[0].lower()
        if key in lookup:
            bound.add(lookup[key])
    return bound


# ---------------------------------------------------------------------------
# Console: colour, clearing, and single-key input
# ---------------------------------------------------------------------------
ANSI_COLORS = {
    'Black': '30', 'DarkBlue': '34', 'DarkGreen': '32', 'DarkCyan': '36',
    'DarkRed': '31', 'DarkMagenta': '35', 'DarkYellow': '33', 'Gray': '37',
    'DarkGray': '90', 'Blue': '94', 'Green': '92', 'Cyan': '96', 'Red': '91',
    'Magenta': '95', 'Yellow': '93', 'White': '97',
}
_USE_COLOR = False
_VT_ENABLED = False


def enable_console_features():
    """Turn on virtual-terminal processing so ANSI colour and hyperlinks work."""
    global _USE_COLOR, _VT_ENABLED
    if os.name == 'nt':
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                if kernel32.SetConsoleMode(handle, mode.value | 0x0004):
                    _VT_ENABLED = True
        except Exception:
            _VT_ENABLED = False
    else:
        _VT_ENABLED = True
    try:
        tty = sys.stdout.isatty()
    except Exception:
        tty = False
    _USE_COLOR = (
        _VT_ENABLED and tty
        and not os.environ.get('NO_COLOR')
        and not (S.args is not None and S.args.NoColor)
    )


def write_host(text='', color=None, newline=True):
    """Write-Host."""
    text = '' if text is None else str(text)
    if color and _USE_COLOR and color in ANSI_COLORS:
        text = '\x1b[{0}m{1}\x1b[0m'.format(ANSI_COLORS[color], text)
    sys.stdout.write(text + ('\n' if newline else ''))
    sys.stdout.flush()


def write_warning(message):
    """Write-Warning."""
    text = 'WARNING: {0}'.format(message)
    if _USE_COLOR:
        text = '\x1b[93m{0}\x1b[0m'.format(text)
    sys.stderr.write(text + '\n')
    sys.stderr.flush()


def write_verbose(message):
    """Write-Verbose."""
    if not (S.args is not None and S.args.Verbose):
        return
    text = 'VERBOSE: {0}'.format(message)
    if _USE_COLOR:
        text = '\x1b[96m{0}\x1b[0m'.format(text)
    sys.stderr.write(text + '\n')
    sys.stderr.flush()


def clear_host():
    """Clear-Host."""
    if _VT_ENABLED:
        sys.stdout.write('\x1b[2J\x1b[3J\x1b[H')
        sys.stdout.flush()
    else:
        os.system('cls' if os.name == 'nt' else 'clear')


# ConsoleKey names, so the menu code reads exactly like the PowerShell original.
_WIN_SPECIAL = {
    'H': 'UpArrow', 'P': 'DownArrow', 'K': 'LeftArrow', 'M': 'RightArrow',
    'I': 'PageUp', 'Q': 'PageDown', 'G': 'Home', 'O': 'End',
    'R': 'Insert', 'S': 'Delete',
}
_VT_SPECIAL = {
    'A': 'UpArrow', 'B': 'DownArrow', 'C': 'RightArrow', 'D': 'LeftArrow',
    'H': 'Home', 'F': 'End',
}
_VT_TILDE = {
    '1': 'Home', '2': 'Insert', '3': 'Delete', '4': 'End',
    '5': 'PageUp', '6': 'PageDown', '7': 'Home', '8': 'End',
}


def _char_to_key(ch):
    if ch in ('\r', '\n'):
        return 'Enter'
    if ch == '\x1b':
        return 'Escape'
    if ch in ('\x08', '\x7f'):
        return 'Backspace'
    if ch == ' ':
        return 'Spacebar'
    if ch == '\t':
        return 'Tab'
    if ch == '/':
        return 'Oem2'
    if ch == '\x03':
        raise KeyboardInterrupt
    if ch.isalpha():
        return ch.upper()
    if ch.isdigit():
        return 'D' + ch
    return ch or 'Unknown'


def read_menu_key(frame=None):
    """Read-MenuKey - the equivalent of [Console]::ReadKey($true)."""
    if os.name == 'nt':
        import msvcrt
        while frame and frame.get('geometry') and not msvcrt.kbhit():
            if menu_geometry() != frame['geometry']:
                return 'Resize'
            time.sleep(0.01)
        ch = msvcrt.getwch()
        if ch in ('\x00', '\xe0'):
            return _WIN_SPECIAL.get(msvcrt.getwch(), 'Unknown')
        return _char_to_key(ch)

    import select
    import termios
    import tty
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)

        def getch(timeout=None):
            if timeout is not None:
                ready, _, _ = select.select([fd], [], [], timeout)
                if not ready:
                    return ''
            return os.read(fd, 1).decode('utf-8', 'replace')

        if frame and frame.get('geometry'):
            while True:
                if menu_geometry() != frame['geometry']:
                    return 'Resize'
                first = getch(0.01)
                if first:
                    break
        else:
            first = getch()
        if first != '\x1b':
            return _char_to_key(first)
        second = getch(0.05)
        if second not in ('[', 'O'):
            return 'Escape'
        third = getch(0.05)
        if third in _VT_SPECIAL:
            return _VT_SPECIAL[third]
        if third.isdigit():
            digits = third
            while True:
                nxt = getch(0.05)
                if not nxt or nxt == '~':
                    break
                digits += nxt
            return _VT_TILDE.get(digits, 'Unknown')
        return 'Unknown'
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def read_host(prompt):
    """Read-Host. Line editing works because raw mode is only held per keypress."""
    try:
        sys.stdout.write(prompt + ': ')
        sys.stdout.flush()
        return sys.stdin.readline().rstrip('\r\n')
    except (EOFError, KeyboardInterrupt):
        return ''


def test_console_input():
    """Test-ConsoleInput.

    Decides whether the menus can run at all. A redirected or non-console host
    cannot serve single-key reads, so the script falls back to the
    non-interactive path instead of throwing.
    """
    try:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            return False
        if os.name == 'nt':
            import msvcrt  # noqa: F401
            return True
        import termios  # noqa: F401
        import tty  # noqa: F401
        return True
    except Exception:
        return False


def get_menu_page_size():
    """Get-MenuPageSize."""
    height = 20
    try:
        lines = shutil.get_terminal_size(fallback=(80, 20)).lines
        if lines > 8:
            height = lines
    except Exception:
        pass
    return max(5, height - 8)


def menu_geometry():
    if (not _VT_ENABLED or not test_console_input()
            or (os.name != 'nt' and os.environ.get('TERM') == 'dumb')):
        return None
    size = shutil.get_terminal_size(fallback=(80, 24))
    return (size.columns, size.lines) if min(size) >= 2 else None


def menu_viewport(count, selected, reserved=5):
    geometry = menu_geometry()
    height = geometry[1] if geometry else get_menu_page_size() + 8
    size = max(1, height - reserved - 1)
    top = selected // size * size
    return top, min(count, top + size), size


def menu_text(text, width):
    # Keep rows on one terminal line and never split a combining sequence.
    text = ''.join(' ' if unicodedata.category(ch) == 'Cc' else ch for ch in str(text))
    result = []
    cells = 0
    for element in _text_elements(text):
        element_cells = sum(0 if unicodedata.combining(ch) or ch in VARIATION_SELECTORS or ch == ZWJ
                            else 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1
                            for ch in element)
        if cells + element_cells > width:
            break
        result.append(element)
        cells += element_cells
    return ''.join(result)


def write_menu_frame(frame, lines, layout):
    geometry = menu_geometry()
    if geometry is None:
        clear_host()
        for text, color in lines:
            write_host(text, color)
        frame.clear()
        return
    width, height = geometry
    visible = list(lines[:height - 1])
    full = (frame.get('geometry') != geometry or frame.get('layout') != layout
            or len(frame.get('lines', [])) != len(visible))
    output = ['\x1b[2J\x1b[H'] if full else []
    for row, (text, color) in enumerate(visible):
        if full or visible[row] != frame['lines'][row]:
            clipped = menu_text(text, width - 1)
            if _USE_COLOR and color in ANSI_COLORS:
                clipped = '\x1b[{}m{}\x1b[0m'.format(ANSI_COLORS[color], clipped)
            output.append('\x1b[{};1H\x1b[2K{}'.format(row + 1, clipped))
    if output:
        output.append('\x1b[{};1H'.format(min(len(visible) + 1, height)))
        sys.stdout.write(''.join(output))
        sys.stdout.flush()
    frame.update(geometry=geometry, lines=visible, layout=layout)


def console_width():
    try:
        return max(20, shutil.get_terminal_size(fallback=(80, 25)).columns - 2)
    except Exception:
        return 78


# ---------------------------------------------------------------------------
# Small comparison helpers
#
# PowerShell string comparison, -in, -contains and ordered-hashtable keys are
# all case-insensitive. These keep that behaviour explicit rather than letting
# Python's case-sensitive dicts quietly change which titles match.
# ---------------------------------------------------------------------------
def in_ci(value, sequence):
    needle = str(value).lower()
    return any(str(item).lower() == needle for item in (sequence or []))


def ci_key(mapping, name):
    """The existing key of an ordered hashtable that matches case-insensitively."""
    if name in mapping:
        return name
    needle = str(name).lower()
    for key in mapping:
        if str(key).lower() == needle:
            return key
    return None


def ci_get(mapping, name, default=None):
    key = ci_key(mapping, name)
    return mapping[key] if key is not None else default


def ci_contains(mapping, name):
    return ci_key(mapping, name) is not None


_SORT_PUNCTUATION = re.compile(r"[-'‘’‐-―_]")


def ps_sort_key(value):
    """Sort-Object's ordering, which is a culture word sort rather than ordinal.

    Punctuation carries less weight than letters and digits, so the LiveChart
    ids - which are negative, and therefore hyphen-prefixed - interleave with
    the AniList ids instead of clustering at the front. Matching it keeps the
    shared database from churning when ics_anime.py and ics_anime.ps1 alternate runs.
    """
    text = str(value)
    return (_SORT_PUNCTUATION.sub('', text).lower(), text.lower())


def sort_unique(items):
    """Sort-Object -Unique over strings: case-insensitive dedupe, then sort."""
    seen = {}
    for item in items or []:
        if item is None:
            continue
        seen.setdefault(str(item).lower(), str(item))
    return [seen[key] for key in sorted(seen, key=ps_sort_key)]


def sort_ci(items, key=None):
    getter = key or (lambda value: value)
    return sorted(items, key=lambda value: ps_sort_key(getter(value) or ''))


def jget(obj, *keys):
    """Nested lookup that tolerates missing links the way PowerShell does."""
    current = obj
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
        if current is None:
            return None
    return current


def as_list(value):
    """PowerShell's @() around a possibly-null, possibly-scalar value."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def to_int(value, default=0):
    try:
        if value is None or value == '':
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Season arithmetic
# ---------------------------------------------------------------------------
class Season:
    __slots__ = ('Season', 'Year')

    def __init__(self, season, year):
        self.Season = str(season)
        self.Year = int(year)

    def key(self):
        return '{0}-{1}'.format(self.Season, self.Year)

    def label(self):
        return '{0} {1}'.format(self.Season, self.Year)

    def __eq__(self, other):
        return isinstance(other, Season) and self.Season == other.Season and self.Year == other.Year

    def __hash__(self):
        return hash((self.Season, self.Year))

    def __repr__(self):
        return 'Season({0} {1})'.format(self.Season, self.Year)


def get_anime_season():
    """Get-AnimeSeason."""
    now = datetime.now()
    if now.month <= 3:
        name = 'WINTER'
    elif now.month <= 6:
        name = 'SPRING'
    elif now.month <= 9:
        name = 'SUMMER'
    else:
        name = 'FALL'
    return Season(name, now.year)


def get_next_anime_season(current):
    """Get-NextAnimeSeason."""
    order = {
        'WINTER': ('SPRING', 0),
        'SPRING': ('SUMMER', 0),
        'SUMMER': ('FALL', 0),
        'FALL': ('WINTER', 1),
    }
    name, bump = order[current.Season]
    return Season(name, current.Year + bump)


# ---------------------------------------------------------------------------
# Text and time helpers
# ---------------------------------------------------------------------------
_BR_TAG = re.compile(r'<br\s*/?>', re.I)
_ANY_TAG = re.compile(r'<[^>]+>', re.S)
_TRAILING_SPACE = re.compile(r'[\t ]+$', re.M)
_EXCESS_EMPTY_LINES = re.compile(r'\n[\t ]*\n(?:[\t ]*\n)+')


def compress_synopsis_whitespace(text):
    """Keep paragraph breaks while collapsing runs of empty lines to one."""
    if not text or not str(text).strip():
        return ''
    value = str(text).replace('\r\n', '\n').replace('\r', '\n')
    return _EXCESS_EMPTY_LINES.sub('\n\n', _TRAILING_SPACE.sub('', value)).strip()


def convert_from_anime_html(text):
    """ConvertFrom-AnimeHtml."""
    if not text:
        return ''
    value = _ANY_TAG.sub('', _BR_TAG.sub(NEWLINE, str(text)))
    return compress_synopsis_whitespace(html.unescape(value))


def html_encode(text):
    """WebUtility.HtmlEncode: entities for markup characters, numeric refs above 159."""
    out = []
    for ch in str(text):
        if ch == '&':
            out.append('&amp;')
        elif ch == '<':
            out.append('&lt;')
        elif ch == '>':
            out.append('&gt;')
        elif ch == '"':
            out.append('&quot;')
        elif ch == "'":
            out.append('&#39;')
        elif ord(ch) > 159:
            out.append('&#{0};'.format(ord(ch)))
        else:
            out.append(ch)
    return ''.join(out)


def convert_to_ics_text(text):
    """ConvertTo-IcsText."""
    if text is None:
        return ''
    return (str(text)
            .replace('\\', '\\\\')
            .replace(';', '\\;')
            .replace(',', '\\,')
            .replace('\r', '')
            .replace('\n', '\\n'))


def _text_elements(value):
    """Grapheme-ish clusters, so folding never splits a combining sequence.

    Stands in for [Globalization.StringInfo]::GetTextElementEnumerator. Python
    strings are code points, so surrogate pairs are already indivisible; what is
    left to keep together are combining marks, variation selectors and
    zero-width-joiner sequences.
    """
    elements = []
    for ch in value:
        joinable = (
            unicodedata.combining(ch) != 0
            or unicodedata.category(ch) in ('Mn', 'Me', 'Mc')
            or ch == ZWJ
            or ch in VARIATION_SELECTORS
        )
        if elements and (joinable or elements[-1].endswith(ZWJ)):
            elements[-1] += ch
        else:
            elements.append(ch)
    return elements


def convert_to_ics_line(line):
    """ConvertTo-IcsLine - RFC 5545 folding at 75 octets.

    Uses a running byte count rather than re-encoding the whole accumulated line
    once per character, which is quadratic over ~1 KB descriptions.
    """
    if len(line.encode('utf-8')) <= 75:
        return line
    parts = []
    count = 0
    first = True
    for element in _text_elements(line):
        size = len(element.encode('utf-8'))
        if not first and (count + size) > 75:
            parts.append(CRLF + ' ')
            count = 1
        parts.append(element)
        count += size
        first = False
    return ''.join(parts)


def convert_to_utc_stamp(value):
    """ConvertTo-UtcStamp."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def round_trip_stamp(value):
    """The .NET round-trip 'o' format, which is what the database stores."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    return value.strftime('%Y-%m-%dT%H:%M:%S.%f') + '0Z'


def _parse_loose(value):
    """The general [datetime]::TryParse fallback, including .NET round-trip 'o'."""
    text = str(value).strip()
    candidate = re.sub(r'(\.\d{1,6})\d*', r'\1', text)
    if candidate.endswith('Z') or candidate.endswith('z'):
        candidate = candidate[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(candidate)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        pass
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d',
                '%m/%d/%Y %H:%M:%S', '%m/%d/%Y'):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def convert_from_anime_date(value, date_only=False, ics=False):
    """ConvertFrom-AnimeDate."""
    if not value:
        return None
    if ics:
        fmt = '%Y%m%d' if date_only else '%Y%m%dT%H%M%SZ'
    else:
        fmt = '%Y-%m-%d' if date_only else '%Y-%m-%dT%H:%M:%SZ'
    try:
        return datetime.strptime(str(value), fmt).replace(tzinfo=UTC)
    except ValueError:
        pass
    if date_only or ics:
        return None
    return _parse_loose(value)


def get_event_instant(entry):
    """Get-EventInstant - one comparable UTC instant for timed and all-day events."""
    if entry.get('AllDay'):
        return convert_from_anime_date(entry.get('Date'), date_only=True)
    return convert_from_anime_date(entry.get('StartUtc'))


_UID_MIGRATION = re.compile(
    r'^(?P<id>-?\d+-(?:\d+|release)-[A-Za-z0-9]+)'
    r'(?:-\d{4}-(?:winter|spring|summer|fall))?'
    r'@anilist-calendar\.local$', re.I)


def get_canonical_event_uid(uid=None, event_id=None):
    """Get-CanonicalEventUid."""
    if event_id:
        return '{0}{1}'.format(event_id, UID_SUFFIX)
    value = str(uid or '').strip()
    # Migrate UIDs written by older versions, which appended the active year and
    # season even when exporting an event loaded from another cached season.
    match = _UID_MIGRATION.match(value)
    if match:
        event_key = match.group('id')
        media = re.match(r'^(-?\d+)(-.+)$', event_key)
        if media:
            event_key = resolve_show_id(media.group(1)) + media.group(2)
        return '{0}{1}'.format(event_key, UID_SUFFIX)
    return value


_DTSTART_LINE = re.compile(r'^DTSTART(?P<date>;VALUE=DATE)?:(?P<v>[^\r\n]+)', re.M)


def get_event_start_from_ics_block(block):
    """Get-EventStartFromIcsBlock."""
    match = _DTSTART_LINE.search(block)
    if not match:
        return None
    return convert_from_anime_date(match.group('v').strip(), ics=True,
                                   date_only=bool(match.group('date')))


def format_show_time(value):
    """PowerShell's 'dddd, MMMM d, yyyy h:mm tt' in the local timezone."""
    local = value.astimezone()
    hour = local.hour % 12 or 12
    return '{0}, {1} {2}, {3} {4}:{5:02d} {6}'.format(
        local.strftime('%A'), local.strftime('%B'), local.day, local.year,
        hour, local.minute, 'AM' if local.hour < 12 else 'PM')


def format_show_date(value):
    """'dddd, MMMM d, yyyy'."""
    return '{0}, {1} {2}, {3}'.format(value.strftime('%A'), value.strftime('%B'),
                                      value.day, value.year)


def format_month(value):
    """'MMMM yyyy'."""
    return '{0} {1}'.format(value.strftime('%B'), value.year)


def format_anime_title(title, providers):
    """Format-AnimeTitle."""
    if isinstance(providers, dict):
        names = list(providers.keys())
    else:
        names = as_list(providers)
    names = sort_unique([name for name in names if name])
    if names:
        return '{0} ({1})'.format(title, ', '.join(names))
    return title


def write_wrapped_text(text):
    """Write-WrappedText."""
    width = console_width()
    for paragraph in re.split(r'\r?\n', str(text or '')):
        if not paragraph.strip():
            write_host('')
            continue
        line = ''
        for word in paragraph.strip().split():
            if not line:
                line = word
            elif len(line) + 1 + len(word) <= width:
                line += ' ' + word
            else:
                write_host(line)
                line = word
        if line:
            write_host(line)


# ---------------------------------------------------------------------------
# AniList client
# ---------------------------------------------------------------------------
ANILIST_ENDPOINT = 'https://graphql.anilist.co'


def invoke_anilist(query, variables=None, maximum_attempts=4):
    """Invoke-AniList.

    AniList allows about 90 requests a minute and answers 429 with Retry-After.
    Without a retry a single rate-limit response aborts an entire scrape with
    nothing saved, so transient failures back off and try again.
    """
    body = json.dumps({'query': query, 'variables': variables or {}},
                      separators=(',', ':')).encode('utf-8')
    headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'User-Agent': USER_AGENT,
    }
    last_error = None
    for attempt in range(1, maximum_attempts + 1):
        status = None
        retry_after = None
        try:
            S.anilist_requests += 1
            request = urllib.request.Request(ANILIST_ENDPOINT, data=body, headers=headers,
                                             method='POST')
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
            text = raw.decode('utf-8', 'replace')
            add_download_bytes('AniList', len(text.encode('utf-8')))
            result = json.loads(text)
            if result.get('errors'):
                messages = [str(error.get('message', error)) for error in as_list(result['errors'])]
                raise RuntimeError('; '.join(messages))
            return result.get('data')
        except urllib.error.HTTPError as error:
            status = error.code
            retry_after = error.headers.get('Retry-After') if error.headers else None
            last_error = error
        except Exception as error:  # network failures and GraphQL errors alike
            last_error = error
        # Mirrors the PowerShell catch: a missing status means a transport-level
        # failure, which is worth another attempt.
        transient = (status == 429 or (status is not None and 500 <= status <= 599)
                     or status is None)
        if not transient or attempt == maximum_attempts:
            raise last_error
        wait = 2 * attempt
        if retry_after:
            parsed = to_int(retry_after, 0)
            if parsed:
                wait = max(parsed, 1)
        write_verbose('AniList attempt {0} failed (status {1}); retrying in {2} s.'
                      .format(attempt, status, wait))
        time.sleep(wait)
    raise last_error if last_error else RuntimeError('AniList request failed.')


def get_season_media_query(include_aired=False):
    """Get-SeasonMediaQuery.

    notYetAired:null makes the API return 500, so the argument is present or
    absent rather than parameterised.
    """
    airing = ('airingSchedule(perPage: 100)' if include_aired
              else 'airingSchedule(notYetAired: true, perPage: 100)')
    return '''query ($page: Int!, $season: MediaSeason!, $year: Int!) {
  Page(page: $page, perPage: 50) {
    pageInfo { hasNextPage }
    media(type: ANIME, season: $season, seasonYear: $year,
      format_in: [TV, TV_SHORT, ONA],
      status_in: [RELEASING, FINISHED, NOT_YET_RELEASED],
      sort: [START_DATE, TITLE_ROMAJI]) {
      id
      siteUrl
      title { romaji english native }
      description(asHtml: false)
      duration
      startDate { year month day }
      endDate { year month day }
      episodes
      genres
      tags { name rank isMediaSpoiler }
      popularity
      status
      externalLinks { site url type }
      ''' + airing + ''' { nodes { episode airingAt } }
      relations {
        edges {
          relationType
          node { id type format description(asHtml: false) title { romaji english } }
        }
      }
    }
  }
}
'''


def get_anilist_media_batch(ids):
    """Get-AniListMediaBatch - one aliased request resolves many ids."""
    wanted = []
    seen = set()
    for value in ids or []:
        number = to_int(value, 0)
        if number > 0 and number not in seen:
            seen.add(number)
            wanted.append(number)
    if not wanted:
        return {}
    results = {}
    for offset in range(0, len(wanted), 10):
        chunk = wanted[offset:offset + 10]
        parts = ['query {']
        for media_id in chunk:
            parts.append('''
  m{0}: Media(id: {0}, type: ANIME) {{
    id
    description(asHtml: false)
    title {{ romaji english }}
    relations {{ edges {{ relationType node {{ id type format description(asHtml: false) title {{ romaji english }} }} }} }}
  }}'''.format(media_id))
        parts.append(os.linesep + '}')
        try:
            data = invoke_anilist(''.join(parts))
        except Exception as error:
            write_verbose('Batch media lookup failed: {0}'.format(error))
            continue
        for value in (data or {}).values():
            if isinstance(value, dict) and value.get('id'):
                results[to_int(value['id'])] = value
    return results


_SEQUEL_HINT = re.compile(
    r'^\s*(the )?(second|third|fourth|final|new|next) season\b'
    r'|\b(sequel|continuation) (of|to)\b'
    r'|\bsee (the )?(first|previous|prior) season\b'
    r'|\bcontinues? (from|the story)\b', re.I)


def test_incomplete_synopsis(synopsis):
    """Test-IncompleteSynopsis."""
    if not synopsis or not str(synopsis).strip() or len(str(synopsis).strip()) < 90:
        return True
    return bool(_SEQUEL_HINT.search(str(synopsis)))


def get_prequel_node(node):
    """Get-PrequelNode."""
    if not node:
        return None
    for edge in as_list(jget(node, 'relations', 'edges')):
        inner = edge.get('node') if isinstance(edge, dict) else None
        if not inner:
            continue
        if (edge.get('relationType') == 'PREQUEL' and inner.get('type') == 'ANIME'
                and inner.get('format') in ('TV', 'TV_SHORT', 'ONA')):
            return inner
    return None


def format_season_one_synopsis(node, text):
    """Return synopsis text obtained by walking back to the first TV season."""
    return text


_SYNOPSIS_SOURCE_PREFIX = re.compile(
    r'^(?:(?:Season 1|Series) synopsis from .{1,200}:\s+)', re.I)


def remove_synopsis_source_prefix(text):
    """Remove source labels written by earlier versions."""
    return _SYNOPSIS_SOURCE_PREFIX.sub('', str(text or ''))


def resolve_series_synopses(media, maximum_depth=6):
    """Resolve-SeriesSynopses.

    Fills in a usable synopsis for every media item.

    A sequel's own AniList description is often "The second season of X", so the
    useful text lives on a prequel. The season query already returns one level of
    relations, which resolves most of them without a request at all; anything
    deeper is walked one hop at a time with all of that hop's ids fetched in a
    single aliased batch. Results are memoised, so a prequel shared by several
    sequels is fetched once.
    """
    synopsis = {}
    pending = {}   # media id -> next prequel id to inspect
    memo = {}      # full media records returned by get_anilist_media_batch
    best = {}      # deepest usable synopsis if the relation chain is incomplete

    for item in media:
        own = convert_from_anime_html(item.get('description'))
        if item.get('MetadataSource') == 'LiveChart':
            synopsis[to_int(item['id'])] = own or (
                'No full synopsis is currently available from LiveChart or AniList.')
            continue
        if not test_incomplete_synopsis(own):
            synopsis[to_int(item['id'])] = own
            continue

        node = get_prequel_node(item)
        if node:
            text = convert_from_anime_html(node.get('description'))
            if not test_incomplete_synopsis(text):
                best[to_int(item['id'])] = (node, text)
            # Relation nodes are shallow. Fetch the prequel itself so its own
            # PREQUEL edge is available and traversal can reach season one.
            pending[to_int(item['id'])] = to_int(node['id'])
        if to_int(item['id']) not in pending:
            synopsis[to_int(item['id'])] = own or (
                'No full synopsis is currently available from AniList.')

    depth = 0
    while depth < maximum_depth and pending:
        wanted = [value for value in set(pending.values()) if to_int(value) not in memo]
        if wanted:
            for key, value in get_anilist_media_batch(wanted).items():
                memo[to_int(key)] = value
        following = {}
        for media_id, node_id in pending.items():
            node = memo.get(to_int(node_id))
            if not node:
                if media_id in best:
                    synopsis[media_id] = format_season_one_synopsis(*best[media_id])
                continue
            text = convert_from_anime_html(node.get('description'))
            if not test_incomplete_synopsis(text):
                best[media_id] = (node, text)
            prequel = get_prequel_node(node)
            if prequel:
                following[media_id] = to_int(prequel['id'])
            elif media_id in best:
                synopsis[media_id] = format_season_one_synopsis(*best[media_id])
        pending = following
        depth += 1

    for media_id in pending:
        if media_id in best:
            synopsis[media_id] = format_season_one_synopsis(*best[media_id])

    for item in media:
        if to_int(item['id']) not in synopsis:
            own = convert_from_anime_html(item.get('description'))
            synopsis[to_int(item['id'])] = own or (
                'No full synopsis is currently available from AniList.')
    return synopsis


# ---------------------------------------------------------------------------
# Provider attribution
# ---------------------------------------------------------------------------
_NON_PROVIDER_HOSTS = re.compile(
    r'(^|\.)(anilist\.co|livechart\.me|twitter\.com|x\.com|youtube\.com|youtu\.be)$', re.I)


def get_provider_from_link(link, allow_host_fallback=False):
    """Get-ProviderFromLink.

    The PowerShell original ends with a fallback that names a provider after the
    link's hostname, but it assigns to $host - a read-only automatic variable -
    so the assignment throws and the branch's own catch returns $null. That
    fallback has therefore never executed.

    Reproducing its apparent intent unconditionally is not an improvement. The
    links that reach it are overwhelmingly the bare hrefs scraped out of
    LiveChart article bodies, which are production-committee homepages rather
    than streaming services: enabling it there turns a 6-entry provider
    directory into 98 entries of noise, and that directory is shared with
    ics_anime.ps1 through anime-ics-db.json.

    So the fallback runs only for AniList links already known to be STREAMING,
    where naming an unrecognised service after its host is what was meant. Every
    other caller gets the behaviour the PowerShell script actually has.
    """
    site = link.get('site') or ''
    url = link.get('url') or ''
    value = '{0} {1}'.format(site, url)
    for name, pattern in PROVIDER_CATALOG:
        if pattern.search(value):
            return name
    if site:
        return str(site).strip()
    if not allow_host_fallback:
        return None
    try:
        hostname = urlparse(str(url)).hostname or ''
        hostname = re.sub(r'^www\.', '', hostname, flags=re.I)
        if _NON_PROVIDER_HOSTS.search(hostname):
            return None
        return hostname or None
    except Exception:
        return None


def get_provider_main_url(url):
    """Get-ProviderMainUrl."""
    if not url:
        return None
    try:
        parsed = urlparse(str(url))
        if parsed.scheme not in ('http', 'https'):
            return None
        if not parsed.netloc:
            return None
        return '{0}://{1}/'.format(parsed.scheme, parsed.netloc)
    except Exception:
        return None


def register_provider(name, url):
    """Register-Provider."""
    if not name:
        return
    main_url = get_provider_main_url(url)
    directory = S.db['ProviderDirectory']
    key = ci_key(directory, name)
    if key is None:
        directory[name] = {'Name': name, 'Url': main_url}
        set_database_dirty()
    elif main_url and directory[key].get('Url') != main_url:
        directory[key]['Url'] = main_url
        set_database_dirty()


_ISEKAI_TAG = re.compile(r'\b(isekai|reincarnat|another world|transmigration)\b', re.I)


def test_fantasy_isekai_reincarnation(media):
    """Test-FantasyIsekaiReincarnation."""
    if in_ci('Fantasy', as_list(media.get('genres'))):
        return True
    for tag in as_list(media.get('tags')):
        if not isinstance(tag, dict):
            continue
        if not tag.get('isMediaSpoiler') and _ISEKAI_TAG.search(str(tag.get('name') or '')):
            return True
    return False


def get_web_page_text(uri):
    """Get-WebPageText."""
    if not uri:
        return ''
    try:
        request = urllib.request.Request(str(uri), headers={'User-Agent': USER_AGENT})
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
        # Windows PowerShell 5.1 sometimes decodes UTF-8 HTML as Windows-1252,
        # producing text such as "I<e2><80><99>m", so the bytes are decoded
        # explicitly here too rather than trusting a declared charset.
        if raw[:3] == b'\xef\xbb\xbf':
            raw = raw[3:]
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            text = raw.decode('utf-8', 'replace')
        add_download_bytes(urlparse(str(uri)).hostname, len(text.encode('utf-8')))
        return text
    except Exception as error:
        write_warning('Lineup page unavailable: {0} ({1})'.format(uri, error))
        return ''


def test_media_on_page(media, page):
    """Test-MediaOnPage.

    Requires a longer title than a four-character floor and checks word
    boundaries, so short titles no longer match arbitrary page text.
    """
    if not page:
        return False
    titles = [jget(media, 'title', 'english'), jget(media, 'title', 'romaji'),
              jget(media, 'title', 'native')]
    for title in titles:
        if not title or len(title) < 8:
            continue
        for candidate in (title, html_encode(title)):
            pattern = '(?<![A-Za-z0-9])' + re.escape(candidate) + '(?![A-Za-z0-9])'
            if re.search(pattern, page, re.I):
                return True
    return False


_LC_FLAGS = re.I | re.S
_LC_ARTICLE = re.compile(r'<article class="anime"(?P<attrs>[^>]*)>(?P<body>[\s\S]*?)</article>',
                         _LC_FLAGS)
_LC_TAGBLOCK = re.compile(r'<ol class="anime-tags">(?P<block>[\s\S]*?)</ol>', _LC_FLAGS)
_LC_TAGLINK = re.compile(r'<a[^>]*>(?P<v>[^<]+)</a>', _LC_FLAGS)
_LC_HREF = re.compile(r'href="(?P<v>https?://[^"]+)"', _LC_FLAGS)


def _lc_group(pattern, text, name='v'):
    match = re.search(pattern, text, _LC_FLAGS)
    return match.group(name) if match else ''


def get_livechart_entries(html_text, page_url):
    """Get-LiveChartEntries."""
    entries = []
    for match in _LC_ARTICLE.finditer(html_text):
        attrs = match.group('attrs')
        body = match.group('body')
        live_id = _lc_group(r'data-anime-id="(?P<v>\d+)"', attrs)
        romaji = html.unescape(_lc_group(r'data-romaji="(?P<v>[^"]*)"', attrs))
        english = html.unescape(_lc_group(r'data-english="(?P<v>[^"]*)"', attrs))
        premiere_text = _lc_group(r'data-premiere="(?P<v>\d+)"', attrs)
        precision = _lc_group(r'data-premiere-precision="(?P<v>\d+)"', attrs)
        anilist_text = _lc_group(r'https://anilist\.co/anime/(?P<v>\d+)', body)
        episode_text = _lc_group(r'release-schedule-info[^>]*>\s*EP(?P<v>\d+)', body)
        synopsis = convert_from_anime_html(
            _lc_group(r'<div class="anime-synopsis"[^>]*>(?P<v>[\s\S]*?)</div>', body))
        if re.match(r'^No synopsis has been added', synopsis, re.I):
            synopsis = ''
        tags = []
        for block in _LC_TAGBLOCK.finditer(body):
            for link in _LC_TAGLINK.finditer(block.group('block')):
                tags.append(html.unescape(link.group('v')))
        provider_links = {}
        for href in _LC_HREF.finditer(body):
            url = html.unescape(href.group('v'))
            provider = get_provider_from_link({'site': '', 'url': url})
            if provider and not ci_contains(provider_links, provider):
                provider_links[provider] = url
        entries.append(SimpleNamespace(
            LiveChartId=live_id,
            AniListId=to_int(anilist_text, 0) or None,
            Romaji=romaji,
            English=english,
            Synopsis=synopsis,
            Tags=tags,
            AiringAt=to_int(premiere_text, 0),
            Precision=to_int(precision, 0),
            Episode=to_int(episode_text, 1) or 1,
            ProviderLinks=provider_links,
            Url='{0}#anime-{1}'.format(page_url, live_id),
        ))
    return entries


def read_csv_rows(path, what):
    """Import-Csv with case-insensitive column access."""
    if not os.path.isfile(path):
        raise FileNotFoundError('{0} not found: {1}'.format(what, path))
    with open(path, 'r', encoding='utf-8-sig', newline='') as handle:
        for row in csv.DictReader(handle):
            yield {str(key or '').strip().lower(): (value or '').strip()
                   for key, value in row.items()}


def get_lineup_sources(season_name, season_year, path):
    """Get-LineupSources."""
    sources = {'LiveChart': 'https://www.livechart.me/{0}-{1}/tv'.format(
        season_name.lower(), season_year)}
    if not path:
        return sources
    if not os.path.exists(path):
        write_warning('Lineup source list not found: {0}'.format(path))
        return sources
    for row in read_csv_rows(path, 'Lineup source list'):
        if row.get('season', '') != season_name:
            continue
        if to_int(row.get('year'), -1) != int(season_year):
            continue
        if row.get('provider') and row.get('url'):
            sources[row['provider']] = row['url']
    return sources


# ---------------------------------------------------------------------------
# Database
#
# The PowerShell script needs ConvertTo-DeepHashtable to turn the PSCustomObject
# tree that ConvertFrom-Json produces into case-insensitive ordered hashtables it
# can index and mutate. json.load already returns ordered, mutable dicts, so that
# function has no counterpart here; the case-insensitive lookups it provided are
# the ci_get/ci_key/ci_contains helpers above.
#
# Shows are stored once and episodes reference them by MediaId. The previous
# schema repeated the full synopsis and a description built from it on every
# episode row, which was 1.07 MB of duplicated text in a 2.1 MB file and made
# each keypress in the picker re-serialise the lot.
# ---------------------------------------------------------------------------
def new_anime_database(default_directory):
    """New-AnimeDatabase."""
    return {
        'Version': DB_VERSION,
        'OutputDirectory': default_directory,
        'Selections': [],
        'SelectionHistory': [],
        'RetainedCalendarShows': [],
        'AppliedSelections': [],
        'KnownShows': [],
        'PendingExport': False,
        'PendingUpdates': [],
        'ScrapeBytesTotal': 0,
        'ScrapeBytesBySource': {},
        'EnabledProviders': [],
        'ProviderDirectory': {},
        'Shows': {},
        'ShowAliases': {},
        'Seasons': {},
    }


def import_anime_database(path, default_directory):
    """Import-AnimeDatabase."""
    database = new_anime_database(default_directory)
    if not os.path.exists(path):
        return database
    try:
        # utf-8-sig so a file written by Windows PowerShell's -Encoding UTF8,
        # which emits a BOM, still parses.
        with open(path, 'r', encoding='utf-8-sig') as handle:
            loaded = json.load(handle)
    except Exception as error:
        write_warning('Database could not be read and will be rebuilt: {0}'.format(error))
        return database
    if not isinstance(loaded, dict):
        write_warning('Database at {0} is not an object; starting a fresh cache.'.format(path))
        return database
    if to_int(ci_get(loaded, 'Version'), -1) != DB_VERSION:
        write_warning('Database at {0} is version {1}; this script writes version {2}. '
                      'Starting a fresh cache.'
                      .format(path, ci_get(loaded, 'Version'), DB_VERSION))
        return database
    for key in ('OutputDirectory', 'PendingExport', 'ScrapeBytesTotal'):
        if ci_contains(loaded, key):
            database[key] = ci_get(loaded, key)
    for key in ('Selections', 'SelectionHistory', 'RetainedCalendarShows', 'AppliedSelections',
                'KnownShows', 'PendingUpdates', 'EnabledProviders'):
        if ci_contains(loaded, key):
            database[key] = as_list(ci_get(loaded, key))
    if not ci_contains(loaded, 'AppliedSelections'):
        database['AppliedSelections'] = list(database['Selections'])
    database['SelectionHistory'] = sort_unique(database['SelectionHistory'] +
                                               database['Selections'] + database['AppliedSelections'])
    for key in ('Shows', 'ShowAliases', 'Seasons', 'ProviderDirectory', 'ScrapeBytesBySource'):
        value = ci_get(loaded, key)
        if ci_contains(loaded, key) and value:
            database[key] = value
    if not database.get('OutputDirectory'):
        database['OutputDirectory'] = default_directory
    return database


def save_anime_database(force=False):
    """Save-AnimeDatabase.

    Writes only when something actually changed. Menu toggles mark the database
    dirty and the flush happens on navigation, so holding a key down in the
    picker no longer serialises the file once per repeat.
    """
    if not S.db_dirty and not force:
        return
    payload = json.dumps(S.db, ensure_ascii=False, default=str, separators=(',', ':'))
    directory = os.path.dirname(S.db_path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    temporary = S.db_path + '.tmp'
    # Written to a sibling then moved, so an interrupted save cannot truncate a
    # database that took a full scrape to build.
    with open(temporary, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(payload)
    os.replace(temporary, S.db_path)
    S.db_dirty = False


def set_database_dirty():
    """Set-DatabaseDirty."""
    S.db_dirty = True


def resolve_show_id(media_id):
    return str((S.db or {}).get('ShowAliases', {}).get(str(media_id), media_id))


def resolve_duplicate_shows():
    """Link unambiguous LiveChart fallbacks to AniList without discarding choices."""
    aliases = S.db.setdefault('ShowAliases', {})
    changed = False
    by_title = {}

    def title_key(show):
        return ' '.join(unicodedata.normalize('NFKC', str(show.get('Title') or '')).split()).lower()

    for show in S.db['Shows'].values():
        if to_int(show.get('MediaId')) > 0 and title_key(show):
            by_title.setdefault(title_key(show), []).append(show)
    for show in S.db['Shows'].values():
        if to_int(show.get('MediaId')) >= 0 or not title_key(show):
            continue
        matches = [candidate for candidate in by_title.get(title_key(show), [])
                   if any(ci_contains(candidate.get('Providers', {}), name)
                          for name in show.get('Providers', {}))]
        if len(matches) == 1 and aliases.get(str(show['MediaId'])) != str(matches[0]['MediaId']):
            aliases[str(show['MediaId'])] = str(matches[0]['MediaId'])
            changed = True
    if not aliases:
        return
    for source_id, target_id in aliases.items():
        source, target = S.db['Shows'].get(source_id), S.db['Shows'].get(target_id)
        if not source or not target:
            continue
        for name, url in source.get('Providers', {}).items():
            if not ci_contains(target['Providers'], name):
                target['Providers'][name] = url
                changed = True
    for field in ('Selections', 'AppliedSelections', 'SelectionHistory', 'KnownShows', 'RetainedCalendarShows'):
        ids = sort_unique([resolve_show_id(value) for value in S.db.get(field, [])])
        if ids != sort_unique(S.db.get(field, [])):
            S.db[field] = ids
            changed = True
            if field in ('Selections', 'AppliedSelections', 'RetainedCalendarShows'):
                S.db['PendingExport'] = True
    native = {(str(entry.get('MediaId')), str(entry.get('Episode'))) for entry in cached_events()
              if resolve_show_id(entry.get('MediaId')) == str(entry.get('MediaId'))}
    native_timed = {str(entry.get('MediaId')) for entry in cached_events()
                    if resolve_show_id(entry.get('MediaId')) == str(entry.get('MediaId')) and not entry.get('AllDay')}
    for season in S.db['Seasons'].values():
        ids = sort_unique([resolve_show_id(value) for value in season.get('ShowIds', [])])
        if ids != sort_unique(season.get('ShowIds', [])):
            season['ShowIds'] = ids
            changed = True
        events = []
        for entry in season.get('Events', []):
            media_id = resolve_show_id(entry.get('MediaId'))
            if media_id != str(entry.get('MediaId')):
                changed = True
                if ((media_id, str(entry.get('Episode'))) in native
                        or (entry.get('AllDay') and media_id in native_timed)):
                    continue
                entry = dict(entry, MediaId=media_id)
            events.append(entry)
        season['Events'] = events
    seen = set()
    pending = []
    for item in S.db['PendingUpdates']:
        media_id = resolve_show_id(item.get('MediaId'))
        if media_id != str(item.get('MediaId')):
            item['MediaId'] = media_id
            changed = True
        if media_id in seen:
            changed = True
            continue
        seen.add(media_id)
        pending.append(item)
    S.db['PendingUpdates'] = pending
    if changed:
        update_selection_history()
        set_database_dirty()


def update_anime_database(change=None, pending=False, save=False):
    """Update-AnimeDatabase."""
    if change:
        change()
    if pending:
        S.db['PendingExport'] = True
    set_database_dirty()
    if save:
        save_anime_database()


# ---------------------------------------------------------------------------
# Scraping a season into the database
# ---------------------------------------------------------------------------
def update_season_cache(season_name, season_year, include_aired=False):
    """Update-SeasonCache."""
    season_key = '{0}-{1}'.format(season_name, season_year)
    write_host('Fetching {0} {1} from AniList...'.format(season_name, season_year), 'DarkGray')

    query = get_season_media_query(include_aired=include_aired)
    media_items = []
    page_number = 1
    while True:
        data = invoke_anilist(query, {'page': page_number, 'season': season_name,
                                      'year': int(season_year)})
        for item in as_list(jget(data, 'Page', 'media')):
            media_items.append(item)
        if not jget(data, 'Page', 'pageInfo', 'hasNextPage'):
            break
        page_number += 1
    write_host('  AniList returned {0} title(s).'.format(len(media_items)), 'DarkGray')

    sources = get_lineup_sources(season_name, season_year, S.args.LineupSourcePath)
    source_text = {}
    for name, url in sources.items():
        if url:
            source_text[name] = get_web_page_text(url)

    # Merge LiveChart by AniList id. This matters most before a season starts,
    # when AniList often has titles but neither provider links nor a schedule.
    live_entries = []
    if source_text.get('LiveChart'):
        live_entries = get_livechart_entries(source_text['LiveChart'], sources['LiveChart'])
        if not live_entries:
            write_warning('LiveChart returned a page but no entries could be parsed. Their '
                          'markup may have changed; provider attribution will rely on AniList '
                          'links alone.')
        else:
            write_host('  LiveChart contributed {0} entry/entries.'.format(len(live_entries)),
                       'DarkGray')

    media_by_id = {to_int(media['id']): media for media in media_items}
    for live in live_entries:
        media = None
        if live.AniListId and to_int(live.AniListId) in media_by_id:
            media = media_by_id[to_int(live.AniListId)]
        if media is not None:
            media['LiveChartProviderLinks'] = live.ProviderLinks
            media['LiveChartUrl'] = live.Url
            if (not as_list(jget(media, 'airingSchedule', 'nodes'))
                    and live.AiringAt > 0 and live.Precision >= 3):
                media.setdefault('airingSchedule', {})
                media['airingSchedule']['nodes'] = [{'episode': live.Episode,
                                                     'airingAt': live.AiringAt}]
            if test_incomplete_synopsis(convert_from_anime_html(media.get('description'))) \
                    and live.Synopsis:
                media['description'] = live.Synopsis
            continue
        if not live.ProviderLinks:
            continue
        start = (datetime.fromtimestamp(live.AiringAt, UTC) if live.AiringAt > 0 else None)
        nodes = ([{'episode': live.Episode, 'airingAt': live.AiringAt}]
                 if live.AiringAt > 0 and live.Precision >= 3 else [])
        media_items.append({
            'id': -1 * to_int(live.LiveChartId),
            'siteUrl': live.Url,
            'MetadataSource': 'LiveChart',
            'title': {'romaji': live.Romaji, 'english': live.English, 'native': ''},
            'description': live.Synopsis,
            'duration': 30,
            'popularity': 0,
            'status': ('RELEASING' if start and start <= datetime.now(UTC)
                       else 'NOT_YET_RELEASED'),
            'genres': list(live.Tags),
            'tags': [{'name': tag, 'rank': 0, 'isMediaSpoiler': False} for tag in live.Tags],
            'externalLinks': [],
            'relations': {'edges': []},
            'LiveChartProviderLinks': live.ProviderLinks,
            'LiveChartUrl': live.Url,
            'startDate': {'year': start.year if start else None,
                          'month': start.month if start else None,
                          'day': start.day if start else None},
            'airingSchedule': {'nodes': nodes},
        })

    overrides = {}
    if S.args.ProviderOverridesPath:
        for row in read_csv_rows(S.args.ProviderOverridesPath, 'Provider override list'):
            if row.get('anilistid') and row.get('provider'):
                overrides[to_int(row['anilistid'])] = row

    write_host('  Resolving synopses...', 'DarkGray')
    synopsis_by_id = resolve_series_synopses(media_items)

    previous_events = {}
    if ci_contains(S.db['Seasons'], season_key):
        for old in as_list(jget(ci_get(S.db['Seasons'], season_key), 'Events')):
            previous_events['{0}|{1}'.format(old.get('MediaId'), old.get('Episode'))] = old

    events = []
    shows = {}
    for media in media_items:
        service_links = {}
        for link in as_list(media.get('externalLinks')):
            if not isinstance(link, dict) or link.get('type') != 'STREAMING':
                continue
            service = get_provider_from_link(link, allow_host_fallback=True)
            register_provider(service, str(link.get('url') or ''))
            if service and not ci_contains(service_links, service):
                service_links[service] = str(link.get('url') or '')
        if 'LiveChartProviderLinks' in media:
            for service, url in (media.get('LiveChartProviderLinks') or {}).items():
                register_provider(service, str(url))
                if not ci_contains(service_links, service):
                    service_links[service] = url
        if to_int(media['id']) in overrides:
            row = overrides[to_int(media['id'])]
            key = ci_key(service_links, row['provider']) or row['provider']
            service_links[key] = row['url']
        for service in sources:
            if service == 'LiveChart':
                continue
            if (not ci_contains(service_links, service)
                    and source_text.get(service)
                    and test_media_on_page(media, source_text[service])):
                service_links[service] = sources[service]
        if not service_links:
            continue

        media_id = str(media['id'])
        title = jget(media, 'title', 'english') or jget(media, 'title', 'romaji') or ''
        shows[media_id] = {
            'MediaId': media_id,
            'Title': title,
            'Synopsis': str(synopsis_by_id.get(to_int(media['id']), '')),
            'IsFantasyGroup': bool(test_fantasy_isekai_reincarnation(media)),
            'SiteUrl': str(media.get('siteUrl') or ''),
            'Popularity': to_int(media.get('popularity'), 0),
            'Status': str(media.get('status') or ''),
            'TotalEpisodes': to_int(media.get('episodes')),
            'EndDate': ('{year:04d}-{month:02d}-{day:02d}'.format(**media['endDate'])
                        if all(jget(media, 'endDate', part) for part in ('year', 'month', 'day'))
                        else None),
            'Providers': service_links,
        }

        duration = to_int(media.get('duration'), 0)
        if duration <= 0:
            duration = 30
        airings = [node for node in as_list(jget(media, 'airingSchedule', 'nodes'))
                   if isinstance(node, dict)]
        for airing in airings:
            start = datetime.fromtimestamp(to_int(airing.get('airingAt')), UTC)
            key = '{0}|{1}'.format(media_id, airing.get('episode'))
            sequence = 0
            if key in previous_events:
                sequence = to_int(previous_events[key].get('Sequence'), 0)
                # A moved airtime must advertise a new SEQUENCE or subscribed
                # clients keep showing the old slot.
                if str(previous_events[key].get('StartUtc')) != convert_to_utc_stamp(start):
                    sequence += 1
            events.append({
                'MediaId': media_id,
                'Episode': str(airing.get('episode')),
                'AllDay': False,
                'StartUtc': convert_to_utc_stamp(start),
                'Date': None,
                'DurationMinutes': duration,
                'Sequence': sequence,
            })
        if not airings and jget(media, 'startDate', 'year') and jget(media, 'startDate', 'month'):
            # Announced shows usually have a month long before they have a day,
            # which is the normal state of an upcoming season. Treating that as
            # "no date" dropped the title from the picker entirely, so a
            # month-only premiere becomes a placeholder on the first of the
            # month, labelled as approximate. When the real date is published the
            # stored date changes, SEQUENCE increments, and subscribed calendars
            # move the entry.
            precise = bool(jget(media, 'startDate', 'day'))
            day = to_int(jget(media, 'startDate', 'day'), 1) if precise else 1
            # Stored as a plain date. Serialising a DateTime here is what made
            # all-day events land a day early east of UTC.
            date_text = '{0:04d}-{1:02d}-{2:02d}'.format(
                to_int(jget(media, 'startDate', 'year')),
                to_int(jget(media, 'startDate', 'month')), day)
            key = '{0}|release'.format(media_id)
            sequence = 0
            if key in previous_events:
                sequence = to_int(previous_events[key].get('Sequence'), 0)
                if str(previous_events[key].get('Date')) != date_text:
                    sequence += 1
            events.append({
                'MediaId': media_id,
                'Episode': 'release',
                'AllDay': True,
                'StartUtc': None,
                'Date': date_text,
                'DurationMinutes': 0,
                'Sequence': sequence,
                'DatePrecision': 'Day' if precise else 'Month',
            })

    for media_id, record in shows.items():
        key = ci_key(S.db['Shows'], media_id) or media_id
        S.db['Shows'][key] = record
    if S.args.AllDiscoveredProviders:
        S.db['EnabledProviders'] = sorted(S.db['ProviderDirectory'].keys(), key=ps_sort_key)
        S.providers = list(S.db['EnabledProviders'])

    # The season records its shows explicitly. Deriving the list from events
    # instead hid every title that has a provider but no published date yet -
    # most of an upcoming season.
    show_ids = sorted(shows.keys(), key=ps_sort_key)
    approximate = len([entry for entry in events if entry.get('DatePrecision') == 'Month'])
    dated_ids = {str(entry['MediaId']) for entry in events}
    undated = len([media_id for media_id in show_ids if str(media_id) not in dated_ids])
    malformed = len([entry for entry in events
                     if not entry.get('MediaId')
                     or (not entry.get('StartUtc') and not entry.get('Date'))])
    # A season keeps being re-checked while any premiere is still approximate or
    # missing, which is what drives the startup update check.
    complete = bool(events) and approximate == 0 and undated == 0 and malformed == 0

    record = {
        'Season': season_name,
        'Year': int(season_year),
        'UpdatedUtc': round_trip_stamp(datetime.now(UTC)),
        'Complete': complete,
        'IncludesAired': bool(include_aired),
        'ProviderCacheComplete': True,
        'ShowIds': show_ids,
        'Events': events,
    }
    existing_key = ci_key(S.db['Seasons'], season_key) or season_key
    S.db['Seasons'][existing_key] = record
    resolve_duplicate_shows()
    set_database_dirty()
    save_anime_database()
    note = ''
    if approximate > 0:
        note += '  {0} premiere(s) are month-only placeholders.'.format(approximate)
    if undated > 0:
        note += '  {0} show(s) have no announced date yet.'.format(undated)
    write_host('  Cached {0} episode(s) across {1} show(s).{2}'
               .format(len(events), len(shows), note), 'DarkGray')
    return record


def get_season_data(season_name, season_year, force_refresh=False):
    """Get-SeasonData."""
    season_key = '{0}-{1}'.format(season_name, season_year)
    cached = ci_get(S.db['Seasons'], season_key)
    needs_aired = bool(S.args.IncludePastEpisodes and cached
                       and not cached.get('IncludesAired'))
    if cached is not None and cached.get('ProviderCacheComplete') and not force_refresh and not needs_aired:
        write_host('Using cached metadata for {0} {1} from {2}.'
                   .format(season_name, season_year, cached.get('UpdatedUtc')), 'DarkGray')
        return cached
    if needs_aired:
        write_host('Cached season has future episodes only; re-scraping to include aired ones.',
                   'DarkGray')
    return update_season_cache(season_name, season_year,
                               include_aired=bool(S.args.IncludePastEpisodes))


def get_season_show_ids(season_data):
    """Get-SeasonShowIds.

    The shows belonging to a season, including any with no dated episode yet.
    Falls back to the event list for caches written before ShowIds existed.
    """
    if not season_data:
        return []
    ids = {}
    for media_id in as_list(season_data.get('ShowIds')):
        if media_id:
            ids[str(media_id)] = True
    if not ids:
        for entry in as_list(season_data.get('Events')):
            ids[str(entry.get('MediaId'))] = True
    month = {'WINTER': 1, 'SPRING': 4, 'SUMMER': 7, 'FALL': 10}.get(season_data.get('Season'))
    if month and season_data.get('Year'):
        start = datetime(int(season_data['Year']), month, 1, tzinfo=UTC)
        following = get_next_anime_season(Season(season_data['Season'], season_data['Year']))
        end = datetime(following.Year, {'WINTER': 1, 'SPRING': 4, 'SUMMER': 7, 'FALL': 10}[following.Season], 1, tzinfo=UTC)
        for entry in cached_events():
            instant = get_event_instant(entry)
            if not entry.get('AllDay') and instant and start <= instant < end:
                ids[str(entry.get('MediaId'))] = True
    return list(ids.keys())


def get_season_shows(season_data, category_filter='All'):
    """Get-SeasonShows - the show-level projection used by the picker."""
    now_utc = datetime.now(UTC)
    by_show = {}
    for media_id in get_season_show_ids(season_data):
        if ci_contains(S.db['Shows'], str(media_id)):
            by_show[str(media_id)] = []
    seen = set()
    for entry in cached_events():
        media_id = str(entry.get('MediaId'))
        if media_id not in by_show:
            continue
        key = (media_id, str(entry.get('Episode')), entry.get('StartUtc'), entry.get('Date'))
        if key in seen:
            continue
        seen.add(key)
        by_show[media_id].append(entry)

    result = []
    for media_id, entries in by_show.items():
        show = ci_get(S.db['Shows'], media_id)
        if not test_show_available(show) or test_show_finished(media_id):
            continue
        instants = [(entry, get_event_instant(entry)) for entry in entries]
        instants = [pair for pair in instants if pair[1]]
        nxt = min((pair for pair in instants if pair[1] >= now_utc),
                  key=lambda pair: pair[1], default=None)
        result.append(SimpleNamespace(
            MediaId=media_id,
            Title=str(show.get('Title') or ''),
            Synopsis=str(show.get('Synopsis') or ''),
            IsFantasyGroup=bool(show.get('IsFantasyGroup')),
            Providers=enabled_show_providers(show),
            NextEvent=nxt[0] if nxt else None,
            NextWhen=nxt[1] if nxt else None,
            EpisodeCount=len(entries),
        ))

    if category_filter == 'FantasyIsekaiReincarnation':
        result = [show for show in result if show.IsFantasyGroup]
    elif category_filter == 'ExcludeFantasyIsekaiReincarnation':
        result = [show for show in result if not show.IsFantasyGroup]
    return sort_ci(result, key=lambda show: show.Title)


# ---------------------------------------------------------------------------
# Menus
# ---------------------------------------------------------------------------
class MenuItem:
    __slots__ = ('Label', 'Value')

    def __init__(self, label, value):
        self.Label = label
        self.Value = value


def cached_events():
    for season in S.db['Seasons'].values():
        yield from as_list(season.get('Events'))


def enabled_show_providers(show):
    return [name for name in (show or {}).get('Providers', {})
            if in_ci(name, S.db['EnabledProviders'])]


def test_show_available(show):
    return bool(enabled_show_providers(show))


def test_show_finished(media_id, now=None, events_by_show=None):
    now = now or datetime.now(UTC)
    show = ci_get(S.db['Shows'], str(media_id))
    if not show:
        return False
    events = (events_by_show.get(str(media_id), []) if events_by_show is not None else
              [entry for entry in cached_events()
               if str(entry.get('MediaId')) == str(media_id) and not entry.get('AllDay')])
    instants = [(entry, get_event_instant(entry)) for entry in events]
    instants = [(entry, instant) for entry, instant in instants if instant]
    if any(instant > now for _, instant in instants):
        return False
    final = [instant for entry, instant in instants
             if to_int(show.get('TotalEpisodes')) > 0
             and str(entry.get('Episode')) == str(show['TotalEpisodes'])]
    if final:
        return max(final).date() < now.date()
    if isinstance(show.get('EndDate'), str) and show['EndDate']:
        try:
            return datetime.strptime(show['EndDate'], '%Y-%m-%d').date() < now.date()
        except ValueError:
            pass
    return show.get('Status') == 'FINISHED'


def update_selection_history():
    history = sort_unique([str(value) for key in ('SelectionHistory', 'Selections', 'AppliedSelections')
                           for value in as_list(S.db.get(key))])
    updates = [item for item in S.db['PendingUpdates'] if str(item.get('MediaId')) not in history]
    if history != S.db.get('SelectionHistory') or updates != S.db['PendingUpdates']:
        S.db['SelectionHistory'] = history
        S.db['PendingUpdates'] = updates
        set_database_dirty()


def get_visible_pending_updates():
    update_selection_history()
    events_by_show = {}
    for entry in cached_events():
        if not entry.get('AllDay'):
            events_by_show.setdefault(str(entry.get('MediaId')), []).append(entry)
    return [item for item in S.db['PendingUpdates']
            if test_show_available(ci_get(S.db['Shows'], str(item.get('MediaId'))))
            and not test_show_finished(item.get('MediaId'), events_by_show=events_by_show)]


def get_season_menu_items(current):
    """Get-SeasonMenuItems."""
    data = ci_get(S.db['Seasons'], current.key())
    # Check explicit membership before scanning all dates for carryover titles.
    ids = [str(value) for value in (data or {}).get('ShowIds', []) if value]
    if not ids:
        ids = get_season_show_ids(data)
    finished = bool(ids)
    if finished:
        events_by_show = {}
        for entry in cached_events():
            if not entry.get('AllDay'):
                events_by_show.setdefault(str(entry.get('MediaId')), []).append(entry)
        finished = all(test_show_finished(media_id, events_by_show=events_by_show) for media_id in ids)
        if finished:
            known = set(ids)
            finished = all(test_show_finished(media_id, events_by_show=events_by_show)
                           for media_id in get_season_show_ids(data) if media_id not in known)
    if finished:
        current = get_next_anime_season(current)
    nxt = get_next_anime_season(current)
    future = get_next_anime_season(nxt)
    return [
        MenuItem('Current - {0}'.format(current.label()), current),
        MenuItem('Next - {0}'.format(nxt.label()), nxt),
        MenuItem('Future - {0}'.format(future.label()), future),
    ]


def resolve_quit_request():
    """Resolve-QuitRequest."""
    if not S.db.get('PendingExport'):
        return 'QUIT'
    clear_host()
    write_host('Changes have not been exported', 'Yellow')
    write_host('{0} E  Export now{0} Q  Quit anyway{0} Left  Back'.format(NEWLINE), 'Gray')
    while True:
        key = read_menu_key()
        if key in ('E', 'Q', 'LeftArrow'):
            break
    if key == 'E':
        return 'EXPORT'
    if key == 'Q':
        return 'QUIT'
    return 'BACK'


def show_key_menu(title, items, selected=0, back=False, footer=None):
    """Show-KeyMenu."""
    if not items:
        return None
    frame = {}
    footer = footer or []
    while True:
        top, end, size = menu_viewport(len(items), selected, 5 + len(footer))
        lines = [(title, 'Cyan'), ('=' * len(title), 'DarkCyan')]
        for index in range(top, end):
            item = items[index]
            marker = '>' if index == selected else ' '
            color = 'Yellow' if index == selected else 'Gray'
            lines.append((' {0} {1}'.format(marker, item.Label), color))
        lines += [('', None)] + [(line, 'DarkGray') for line in footer]
        lines.append(('Up/Down Move   Right/Enter/Space Select   Left Back   Q Quit', 'DarkGray'))
        write_menu_frame(frame, lines, (top, size))
        key = read_menu_key(frame)
        if key == 'Q':
            frame.clear()
            answer = resolve_quit_request()
            if answer != 'BACK':
                return answer
        elif key == 'UpArrow':
            selected = (selected - 1 + len(items)) % len(items)
        elif key == 'DownArrow':
            selected = (selected + 1) % len(items)
        elif key == 'Home':
            selected = 0
        elif key == 'End':
            selected = len(items) - 1
        elif key in ('RightArrow', 'Enter', 'Spacebar'):
            return items[selected].Value
        elif back and key == 'LeftArrow':
            return None


def show_genre_menu(title):
    """Show-GenreMenu."""
    return show_key_menu(title, [MenuItem(label, value) for label, value in GENRE_ITEMS],
                         back=True)


def show_show_details(show, excluded):
    """Show-ShowDetails."""
    while True:
        clear_host()
        display_title = format_anime_title(show.Title, show.Providers)
        write_host(display_title, 'Cyan')
        write_host('=' * len(display_title), 'DarkCyan')
        state = 'Not in calendar' if excluded else 'In calendar'
        write_host('Status       : {0}'.format(state))
        write_host('Streaming on : {0}'.format(', '.join(as_list(show.Providers))))
        when = format_show_time(show.NextWhen) if show.NextWhen else 'Not announced yet'
        if show.NextEvent and show.NextEvent.get('AllDay'):
            stamp = convert_from_anime_date(show.NextEvent.get('Date'), date_only=True)
            if stamp is None:
                when = 'Not announced yet'
            elif show.NextEvent.get('DatePrecision') == 'Month':
                when = format_month(stamp) + ' - exact date not announced'
            else:
                when = format_show_date(stamp) + ' (date only)'
        write_host('Next release : {0}'.format(when), 'Yellow')
        write_host('')
        write_host('Synopsis', 'DarkCyan')
        write_host('--------', 'DarkCyan')
        write_wrapped_text(show.Synopsis if show.Synopsis
                           else 'No synopsis is currently available.')
        write_host('')
        write_host('Left Back   Q Quit', 'DarkGray')
        key = read_menu_key()
        if key == 'Q':
            answer = resolve_quit_request()
            if answer != 'BACK':
                return answer
        elif key == 'LeftArrow':
            return None


def remove_disabled_provider_data():
    """Remove-DisabledProviderData."""
    update_selection_history()
    S.db['Selections'] = [value for value in S.db['Selections']
                           if test_show_available(ci_get(S.db['Shows'], str(value)))]
    update_anime_database(pending=True)


def refresh_provider_seasons(current):
    """Refresh-ProviderSeasons."""
    S.providers = list(S.db['EnabledProviders'])
    remove_disabled_provider_data()
    cached_shows = 0
    periods = {period.key(): period for period in (current, get_next_anime_season(current))}
    for cached in S.db['Seasons'].values():
        period = Season(cached['Season'], cached['Year'])
        periods[period.key()] = period
    for period in periods.values():
        data = update_season_cache(period.Season, period.Year,
                                   include_aired=bool(S.args.IncludePastEpisodes))
        cached_shows += len(get_season_show_ids(data))
    if as_list(S.db['EnabledProviders']) and cached_shows == 0:
        raise RuntimeError(
            'Provider update returned no shows for the enabled providers: {0}. The previous '
            'calendar remains available for merging; try Update again later.'
            .format(', '.join(as_list(S.db['EnabledProviders']))))
    save_anime_database()


def show_provider_details(provider, current):
    """Show-ProviderDetails."""
    season = ci_get(S.db['Seasons'], current.key())
    top = []
    if season:
        candidates = [show for show in get_season_shows(season)
                      if in_ci(provider['Name'], show.Providers) and show.NextWhen
                      and (ci_get(S.db['Shows'], show.MediaId) or {}).get('Status') == 'RELEASING']
        top = sorted(candidates,
                     key=lambda show: to_int((ci_get(S.db['Shows'], show.MediaId) or {})
                                             .get('Popularity'), 0),
                     reverse=True)[:5]
    selected = 0
    frame = {}
    while True:
        start, end, size = menu_viewport(len(top), selected, 7)
        lines = [(provider['Name'], 'Cyan'), ('', None),
                 ('Website: ' + (provider.get('Url') or 'Not available'), 'Blue'),
                 ('', None), ('Popular currently airing shows', 'DarkCyan')]
        if not top:
            lines.append(('  Not available', None))
        else:
            for index in range(start, end):
                show = top[index]
                name = format_anime_title(show.Title, show.Providers)
                lines.append((' {0} {1}'.format('>' if index == selected else ' ', name),
                              'Yellow' if index == selected else 'Gray'))
        lines += [('', None), ('Up/Down Move   Right Show details   O Open website   Left Back   Q Quit', 'DarkGray')]
        write_menu_frame(frame, lines, (start, size))
        key = read_menu_key(frame)
        if key in ('RightArrow', 'Q', 'O'):
            frame.clear()
        if key == 'UpArrow' and top:
            selected = (selected - 1 + len(top)) % len(top)
        elif key == 'DownArrow' and top:
            selected = (selected + 1) % len(top)
        elif key == 'RightArrow' and top:
            answer = show_show_details(top[selected], False)
            if answer:
                return answer
        elif key == 'O' and provider.get('Url'):
            try:
                webbrowser.open(provider['Url'])
            except Exception as error:
                write_warning('Could not open {0}: {1}'.format(provider['Url'], error))
        elif key == 'LeftArrow':
            return None
        elif key == 'Q':
            return resolve_quit_request()


def show_providers_menu(current):
    """Show-ProvidersMenu."""
    selected = 0
    frame = {}
    while True:
        providers = sort_ci(list(S.db['ProviderDirectory'].values()),
                            key=lambda entry: entry.get('Name'))
        listing = [SimpleNamespace(Name='Update providers and shows', Url=None, IsUpdate=True)]
        listing += [SimpleNamespace(Name=entry.get('Name'), Url=entry.get('Url'), IsUpdate=False)
                    for entry in providers]
        enabled = {str(name).lower() for name in as_list(S.db['EnabledProviders'])}
        top, end, size = menu_viewport(len(listing), selected, 5)
        lines = [('Providers', 'Cyan')]
        for index in range(top, end):
            item = listing[index]
            if item.IsUpdate:
                mark = '[>]'
            elif str(item.Name).lower() in enabled:
                mark = '[x]'
            else:
                mark = '[ ]'
            lines.append((' {0} {1} {2}'.format('>' if index == selected else ' ', mark, item.Name),
                          'Yellow' if index == selected else 'Gray'))
        if not providers:
            lines.append(('Select Update once to discover providers reported by AniList and LiveChart.', 'DarkGray'))
        lines += [('', None), ('Up/Down Move   Space Toggle/Update   Right Provider details   Left Back   Q Quit', 'DarkGray')]
        write_menu_frame(frame, lines, (top, size, tuple(item.Name for item in listing)))
        key = read_menu_key(frame)
        if key in ('RightArrow', 'Q', 'Spacebar'):
            frame.clear()
        if key == 'UpArrow' and listing:
            selected = (selected - 1 + len(listing)) % len(listing)
        elif key == 'DownArrow' and listing:
            selected = (selected + 1) % len(listing)
        elif key == 'Spacebar' and listing:
            if listing[selected].IsUpdate:
                try:
                    refresh_provider_seasons(current)
                except Exception as error:
                    write_warning('Provider update failed: {0}'.format(error))
                    write_host('Press any key to continue.', 'DarkGray')
                    read_menu_key()
                continue
            name = listing[selected].Name
            remove_from_ics = False
            if str(name).lower() in enabled:
                affected = sort_ci([ci_get(S.db['Shows'], str(value)) for value in S.db['Selections']
                                    if ci_contains((ci_get(S.db['Shows'], str(value)) or {}).get('Providers', {}), name)],
                                   key=lambda show: show.get('Title'))
                if affected and not confirm_provider_removal(name, affected):
                    continue
                if affected:
                    remove_from_ics = confirm_provider_calendar_removal(name)
                S.db['EnabledProviders'] = [value for value in as_list(S.db['EnabledProviders'])
                                            if str(value).lower() != str(name).lower()]
                exclusive_ids = [str(show['MediaId']) for show in affected if not test_show_available(show)]
                retained = S.db.get('RetainedCalendarShows', [])
                S.db['RetainedCalendarShows'] = ([value for value in retained if value not in exclusive_ids]
                                                if remove_from_ics else sort_unique(retained + exclusive_ids))
                remove_disabled_provider_data()
            else:
                if any(not record.get('ProviderCacheComplete') for record in S.db['Seasons'].values()):
                    try:
                        refresh_provider_seasons(current)
                    except Exception as error:
                        write_warning('Provider update failed: {}'.format(error))
                        read_menu_key()
                        continue
                S.db['EnabledProviders'] = sort_unique(
                    as_list(S.db['EnabledProviders']) + [name])
                restored = [value for value in S.db.get('RetainedCalendarShows', [])
                            if test_show_available(ci_get(S.db['Shows'], str(value)))]
                S.db['Selections'] = sort_unique(S.db['Selections'] + restored)
                S.db['RetainedCalendarShows'] = [value for value in S.db.get('RetainedCalendarShows', [])
                                                 if value not in restored]
            S.providers = list(S.db['EnabledProviders'])
            update_anime_database(pending=True, save=True)
            if remove_from_ics:
                return 'EXPORT'
        elif key == 'RightArrow' and listing and not listing[selected].IsUpdate:
            answer = show_provider_details({'Name': listing[selected].Name,
                                            'Url': listing[selected].Url}, current)
            if answer in ('QUIT', 'EXPORT'):
                return answer
        elif key == 'LeftArrow':
            return None
        elif key == 'Q':
            return resolve_quit_request()


def confirm_provider_calendar_removal(name):
    return show_key_menu('Also remove {} shows from the ICS?'.format(name), [
        MenuItem('No - keep existing ICS entries', 'NO'),
        MenuItem('Yes - remove and update ICS now', 'YES')], back=True,
        footer=['Shows on another enabled provider stay selected.',
                'Yes exports all pending calendar changes. No preserves existing entries for shows losing their last provider.']) == 'YES'


def confirm_provider_removal(name, shows):
    page = 0
    size = max(1, get_menu_page_size() - 7)
    while True:
        footer = ['Selected shows (shows on another enabled provider stay selected):']
        footer += ['  ' + show['Title'] for show in shows[page * size:(page + 1) * size]]
        items = [MenuItem('No - keep provider', 'NO'), MenuItem('Yes - remove provider', 'YES')]
        if (page + 1) * size < len(shows):
            items.append(MenuItem('Next page of selected shows', 'NEXT'))
        if page:
            items.append(MenuItem('Previous page of selected shows', 'PREVIOUS'))
        answer = show_key_menu('Remove {}? ({} selected shows)'.format(name, len(shows)),
                               items, back=True, footer=footer)
        if answer == 'NEXT':
            page += 1
        elif answer == 'PREVIOUS':
            page -= 1
        else:
            return answer == 'YES'


def save_anime_exclusion(excluded, mark_pending=False):
    """Save-AnimeExclusion.

    The exclusion set includes newly discovered titles until explicitly added.
    """
    all_ids = {}
    for season in S.db['Seasons'].values():
        for media_id in get_season_show_ids(season):
            all_ids[str(media_id)] = True
    S.db['Selections'] = sorted([media_id for media_id in all_ids
                                 if media_id not in excluded], key=ps_sort_key)
    update_selection_history()
    update_anime_database(pending=mark_pending)


def has_show_selection_modifications():
    """Whether pending show choices differ from the last successful ICS export."""
    pending = {str(value) for value in as_list(S.db.get('Selections'))}
    applied = {str(value) for value in as_list(S.db.get('AppliedSelections'))}
    return pending != applied


def show_toggle_list(shows, excluded, applied_excluded, show_excluded, title):
    """Show-ToggleList."""
    selected = 0
    search = ''
    frame = {}
    while True:
        listing = [show for show in shows
                   if (show.MediaId in applied_excluded) == show_excluded]
        if search:
            needle = search.lower()
            listing = [show for show in listing
                       if show.Title and needle in show.Title.lower()]
        lines = [(title, 'Cyan'), ('=' * len(title), 'DarkCyan')]
        if search:
            lines.append(('Filter: {0}'.format(search), 'Magenta'))

        if not listing:
            lines += [('', None), ('Nothing matches that filter.' if search else 'No shows in this list.', None),
                      ('', None), ('/ Filter   C Clear filter   Left Back   Q Quit', 'DarkGray')]
            write_menu_frame(frame, lines, ('empty', search))
            key = read_menu_key(frame)
            if key in ('Q', 'Oem2', 'Divide'):
                frame.clear()
            if key == 'Q':
                answer = resolve_quit_request()
                if answer != 'BACK':
                    return answer
                continue
            if key == 'C':
                search = ''
                continue
            if key in ('Oem2', 'Divide'):
                write_host('')
                search = read_host('Filter')
                selected = 0
                continue
            if key == 'LeftArrow':
                return None
            continue

        if selected >= len(listing):
            selected = len(listing) - 1
        if selected < 0:
            selected = 0
        top, end, page_size = menu_viewport(len(listing), selected, 4 + bool(search))
        for index in range(top, end):
            cursor = '>' if index == selected else ' '
            mark = '[ ] ' if listing[index].MediaId in excluded else '[x] '
            color = 'Yellow' if index == selected else 'Gray'
            lines.append((' {0} {1}{2}'.format(
                cursor, mark, format_anime_title(listing[index].Title, listing[index].Providers)),
                color))
        lines += [('', None), ('{0}-{1}/{2}   Up/Down Move   PgUp/PgDn   Space Toggle   Right Details   '
                               '/ Filter   C Clear   Left Back'.format(top + 1, end, len(listing)), 'DarkGray')]
        write_menu_frame(frame, lines, (top, page_size, search, tuple(show.MediaId for show in listing)))

        key = read_menu_key(frame)
        if key in ('Q', 'RightArrow', 'Oem2', 'Divide'):
            frame.clear()
        if key == 'UpArrow':
            selected = (selected - 1 + len(listing)) % len(listing)
        elif key == 'DownArrow':
            selected = (selected + 1) % len(listing)
        elif key == 'PageUp':
            selected = max(0, selected - page_size)
        elif key == 'PageDown':
            selected = min(len(listing) - 1, selected + page_size)
        elif key == 'Home':
            selected = 0
        elif key == 'End':
            selected = len(listing) - 1
        elif key == 'Spacebar':
            show = listing[selected]
            if show.MediaId in excluded:
                excluded.pop(show.MediaId, None)
            else:
                excluded[show.MediaId] = show.Title
            save_anime_exclusion(excluded, mark_pending=True)
        elif key == 'C':
            search = ''
            selected = 0
        elif key == 'Q':
            answer = resolve_quit_request()
            if answer != 'BACK':
                save_anime_database()
                return answer
        elif key == 'RightArrow':
            answer = show_show_details(listing[selected], listing[selected].MediaId in excluded)
            if answer in ('QUIT', 'EXPORT'):
                save_anime_database()
                return answer
        elif key in ('Oem2', 'Divide'):
            write_host('')
            search = read_host('Filter')
            selected = 0
        elif key == 'LeftArrow':
            save_anime_database()
            return None


def show_management_menu(shows, excluded, season_label):
    """Show-ManagementMenu."""
    while True:
        shows = [show for show in shows
                 if show is not None and str(show.MediaId or '').strip()]
        applied_ids = {str(value) for value in as_list(S.db['AppliedSelections'])}
        applied_excluded = {show.MediaId: show.Title for show in shows
                            if show.MediaId not in applied_ids}
        in_calendar = len([show for show in shows if show.MediaId not in applied_excluded])
        outside = len([show for show in shows if show.MediaId in applied_excluded])
        items = [
            MenuItem('Shows in the calendar ({0})'.format(in_calendar), 'CURRENT'),
            MenuItem('Shows not added ({0})'.format(outside), 'EXCLUDED'),
        ]
        items.append(MenuItem('Update ICS with modifications' if has_show_selection_modifications()
                              else 'Export calendar', 'EXPORT'))
        choice = show_key_menu('Manage {0} shows'.format(season_label), items, back=True)
        if choice in ('QUIT', 'EXPORT'):
            return choice
        if choice is None:
            return 'GENRE'
        if choice == 'CURRENT':
            answer = show_toggle_list(shows, excluded, applied_excluded, False,
                                      'Shows in the calendar - Space toggles pending changes')
            if answer in ('QUIT', 'EXPORT'):
                return answer
        elif choice == 'EXCLUDED':
            answer = show_toggle_list(shows, excluded, applied_excluded, True,
                                      'Shows not added - Space toggles pending changes')
            if answer in ('QUIT', 'EXPORT'):
                return answer


def select_calendar_folder(start):
    """Select-CalendarFolder."""
    folder = start
    selected = 0
    while True:
        try:
            directories = sort_ci([entry for entry in os.scandir(folder) if entry.is_dir()],
                                  key=lambda entry: entry.name)
        except OSError:
            directories = []
        items = [
            MenuItem('[ Save in this folder ]', 'SAVE'),
            MenuItem('[..] Parent folder', 'UP'),
        ]
        items += [MenuItem('[{0}]'.format(entry.name), entry.path) for entry in directories]
        choice = show_key_menu('Output folder: {0}'.format(folder), items,
                               selected=min(selected, len(items) - 1), back=True)
        if choice in ('QUIT', 'EXPORT'):
            return choice
        if choice is None:
            return None
        if choice == 'SAVE':
            return folder
        if choice == 'UP':
            parent = os.path.dirname(os.path.abspath(folder).rstrip(os.sep))
            if parent and parent != folder:
                folder = parent
            selected = 0
            continue
        folder = choice
        selected = 0


def get_week_airings(now=None, tz=None):
    """Selected episodes from now through the end of the next six local days.

    tz=None uses the computer's timezone for each instant, including DST changes.
    Date-only releases keep their published date instead of shifting from UTC.
    """
    now = now or datetime.now(UTC)
    today = now.astimezone(tz).date()
    end = today + timedelta(days=7)
    selected = {resolve_show_id(value) for value in S.db['Selections']}
    airings = {}
    for entry in cached_events():
        media_id = resolve_show_id(entry.get('MediaId'))
        if media_id not in selected:
            continue
        show = ci_get(S.db['Shows'], media_id)
        if not test_show_available(show):
            continue
        if entry.get('AllDay'):
            if entry.get('DatePrecision') == 'Month':
                continue
            date = convert_from_anime_date(entry.get('Date'), date_only=True)
            if date is None:
                continue
            local = date.replace(tzinfo=None)
            time_label = 'TBA'
            sort_time = datetime.max.replace(tzinfo=UTC)
        else:
            instant = get_event_instant(entry)
            if instant is None or instant < now:
                continue
            local = instant.astimezone(tz)
            time_label = local.strftime('%I:%M %p').lstrip('0')
            sort_time = instant
        if not today <= local.date() < end:
            continue
        key = (media_id, str(entry.get('Episode')))
        item = SimpleNamespace(MediaId=media_id, Title=show.get('Title') or '',
                               Episode=str(entry.get('Episode') or ''), Providers=enabled_show_providers(show),
                               Day=local.date(), LocalTime=local, TimeLabel=time_label,
                               SortTime=sort_time, Sequence=to_int(entry.get('Sequence')))
        if key not in airings or item.Sequence > airings[key].Sequence:
            airings[key] = item
    return sorted(airings.values(), key=lambda item: (item.Day, item.SortTime, item.Title, item.Episode))


def get_week_airing_rows(airings):
    rows = []
    day = None
    for airing in airings:
        # Keep weekday/date headers when a long day continues on another page.
        label = '{}, {} {}'.format(airing.Day.strftime('%A'), airing.Day.strftime('%B'), airing.Day.day)
        if day != airing.Day:
            rows.append(SimpleNamespace(Text=label, Color='Cyan', DayLabel=label, IsHeader=True))
            day = airing.Day
        episode = ' - Episode ' + airing.Episode if airing.Episode and airing.Episode != 'release' else ''
        title = format_anime_title(airing.Title, airing.Providers)
        rows.append(SimpleNamespace(Text='  {}  {}{}'.format(airing.TimeLabel, title, episode),
                                    Color='Gray', DayLabel=label, IsHeader=False))
    return rows


def show_week_airings():
    frame = {}
    offset = 0
    refresh = True
    rows = []
    while True:
        if refresh:
            rows = get_week_airing_rows(get_week_airings())
            refresh = False
            offset = 0
            frame.clear()
        _, _, size = menu_viewport(len(rows), 0, 5)
        last = max(0, len(rows) - size)
        offset = min(offset, last)
        lines = [('Airing this week (local time)', 'Cyan'),
                 ('Today and the next six days; unknown airing times are TBA.', 'DarkGray')]
        if not rows:
            lines.append(('No selected shows have dated episodes scheduled in the next 7 days.', None))
        else:
            if not rows[offset].IsHeader:
                lines.append((rows[offset].DayLabel + ' (continued)', 'Cyan'))
            lines.extend((row.Text, row.Color) for row in rows[offset:offset + size])
        lines += [('', None), ('Up/Down Scroll   PgUp/PgDn   Home/End   R Refresh   Left Back   Q Quit', 'DarkGray')]
        write_menu_frame(frame, lines, (offset, size))
        key = read_menu_key(frame)
        if key == 'UpArrow':
            offset = max(0, offset - 1)
        elif key == 'DownArrow':
            offset = min(last, offset + 1)
        elif key == 'PageUp':
            offset = max(0, offset - size)
        elif key == 'PageDown':
            offset = min(last, offset + size)
        elif key == 'Home':
            offset = 0
        elif key == 'End':
            offset = last
        elif key == 'R':
            refresh = True
        elif key in ('LeftArrow', 'Escape'):
            return None
        elif key == 'Q':
            frame.clear()
            answer = resolve_quit_request()
            if answer != 'BACK':
                return answer


def show_pending_updates():
    """Show-PendingUpdates."""
    selected_ids = {str(media_id): True for media_id in as_list(S.db['Selections'])}
    selected = 0
    frame = {}
    review_date = None
    items = []
    labels = {}
    layout_ids = ()
    while True:
        # The menu owns this snapshot; navigation never needs to scan the cache.
        # Date rollover still rechecks completion when the next key is handled.
        today = datetime.now(UTC).date()
        if review_date != today:
            items = sort_ci(get_visible_pending_updates(), key=lambda item: (item or {}).get('Title'))
            labels = {}
            for item in items:
                media_id = str(item.get('MediaId'))
                record = ci_get(S.db['Shows'], media_id)
                name = format_anime_title(item.get('Title'), enabled_show_providers(record))
                labels[media_id] = '{} - {}'.format(name, item.get('Reason'))
            layout_ids = tuple(str(item.get('MediaId')) for item in items)
            review_date = today
        if not items:
            return None
        if selected >= len(items):
            selected = len(items) - 1
        top, end, size = menu_viewport(len(items), selected, 4)
        lines = [('Updated next-season shows', 'Cyan'), ('=========================', 'DarkCyan')]
        for index in range(top, end):
            item = items[index]
            media_id = str(item.get('MediaId'))
            mark = '[x]' if media_id in selected_ids else '[ ]'
            cursor = '>' if index == selected else ' '
            color = 'Yellow' if index == selected else 'Gray'
            lines.append((' {0} {1} {2}'.format(cursor, mark, labels[media_id]), color))
        lines += [('', None), ('Up/Down Move   Space Add/Remove   Right Details   D Dismiss all   Left Back', 'DarkGray')]
        write_menu_frame(frame, lines, (top, size, layout_ids))
        key = read_menu_key(frame)
        if key in ('Q', 'RightArrow'):
            frame.clear()
        if key == 'UpArrow':
            selected = (selected - 1 + len(items)) % len(items)
        elif key == 'DownArrow':
            selected = (selected + 1) % len(items)
        elif key == 'Spacebar':
            media_id = str(items[selected].get('MediaId'))
            if media_id in selected_ids:
                del selected_ids[media_id]
                S.db['Selections'] = [value for value in as_list(S.db['Selections'])
                                      if str(value) != media_id]
            else:
                selected_ids[media_id] = True
                S.db['Selections'] = sort_unique(as_list(S.db['Selections']) + [media_id])
            S.db['PendingExport'] = True
            update_selection_history()
            history = set(S.db['SelectionHistory'])
            items = [item for item in items if str(item.get('MediaId')) not in history]
            layout_ids = tuple(str(item.get('MediaId')) for item in items)
            set_database_dirty()
        elif key == 'D':
            S.db['PendingUpdates'] = []
            set_database_dirty()
            save_anime_database()
            return None
        elif key == 'Q':
            answer = resolve_quit_request()
            if answer != 'BACK':
                save_anime_database()
                return answer
        elif key == 'RightArrow':
            item = items[selected]
            record = ci_get(S.db['Shows'], str(item.get('MediaId')))
            projection = SimpleNamespace(
                MediaId=str(item.get('MediaId')),
                Title=str(item.get('Title') or ''),
                Synopsis=str(record.get('Synopsis') or '') if record else '',
                Providers=enabled_show_providers(record),
                NextEvent=None,
                NextWhen=convert_from_anime_date(item.get('NewStart')),
            )
            answer = show_show_details(projection,
                                       str(item.get('MediaId')) not in selected_ids)
            if answer in ('QUIT', 'EXPORT'):
                save_anime_database()
                return answer
        elif key == 'LeftArrow':
            save_anime_database()
            return None


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def new_calendar_event(entry, show):
    """New-CalendarEvent.

    Expands a stored episode into the events the calendar will carry. The
    description is built here from the show record rather than stored on every
    episode row.
    """
    show_providers = [name for name in (show.get('Providers') or {})
                      if in_ci(name, S.providers)]
    if not show_providers:
        return []
    episode_label = ('' if entry.get('Episode') == 'release'
                     else ' - Episode {0}'.format(entry.get('Episode')))
    approximate = entry.get('DatePrecision') == 'Month'
    if approximate:
        episode_label = ' (date TBA)'
    groups = []
    if S.args.CombineProviders:
        groups.append(list(show_providers))
    else:
        groups.extend([[provider] for provider in show_providers])

    providers_map = show.get('Providers') or {}
    results = []
    for group in groups:
        slug = 'all' if S.args.CombineProviders else re.sub(r'[^A-Za-z0-9]', '', str(group[0]))
        primary_url = ci_get(providers_map, group[0]) or show.get('SiteUrl')
        lines = [str(show.get('Synopsis') or ''), '']
        lines.append('Streaming: {0}'.format(', '.join(group)))
        lines.append('AniList: {0}'.format(show.get('SiteUrl') or ''))
        if approximate:
            stamp = convert_from_anime_date(entry.get('Date'), date_only=True)
            month = format_month(stamp) if stamp else 'The listed month'
            lines.append('Premiere date has not been announced. {0} is the month listed by '
                         'AniList; this entry sits on the 1st as a placeholder and moves once '
                         'the exact date is published.'.format(month))
        elif entry.get('Episode') == 'release':
            lines.append('No episode-level airtime is published; this is the listed release '
                         'date.')
        for provider in group:
            url = ci_get(providers_map, provider)
            if url and url != show.get('SiteUrl'):
                lines.append('Watch/lineup ({0}): {1}'.format(provider, url))
        results.append(SimpleNamespace(
            Uid=get_canonical_event_uid(event_id='{0}-{1}-{2}'.format(
                entry.get('MediaId'), entry.get('Episode'), slug)),
            MediaId=str(entry.get('MediaId')),
            Summary='{0}{1}'.format(format_anime_title(show.get('Title'), group), episode_label),
            Description=NEWLINE.join(lines),
            Url=primary_url,
            AllDay=bool(entry.get('AllDay')),
            Start=get_event_instant(entry),
            Minutes=to_int(entry.get('DurationMinutes'), 0),
            Sequence=to_int(entry.get('Sequence'), 0),
        ))
    return results


def add_ics_event(calendar, entry, stamp):
    """Add-IcsEvent."""
    date = entry.Start.strftime('%Y%m%d')
    timed = entry.Start.strftime('%Y%m%dT%H%M%SZ')
    minutes = entry.Minutes if entry.Minutes > 0 else 30
    fields = [('UID', entry.Uid), ('DTSTAMP', stamp), ('SEQUENCE', entry.Sequence),
              ('LAST-MODIFIED', stamp)]
    if entry.AllDay:
        fields.append(('DTSTART;VALUE=DATE', date))
        fields.append(('DTEND;VALUE=DATE', (entry.Start + timedelta(days=1)).strftime('%Y%m%d')))
    else:
        fields.append(('DTSTART', timed))
        fields.append(('DTEND', (entry.Start + timedelta(minutes=minutes))
                       .strftime('%Y%m%dT%H%M%SZ')))
    fields.append(('SUMMARY', convert_to_ics_text(entry.Summary)))
    fields.append(('DESCRIPTION', convert_to_ics_text(entry.Description)))
    if entry.Url:
        fields.append(('URL', convert_to_ics_text(entry.Url)))
    fields.append(('STATUS', 'CONFIRMED'))
    fields.append(('TRANSP', 'TRANSPARENT'))
    calendar.append('BEGIN:VEVENT')
    for name, value in fields:
        calendar.append(convert_to_ics_line('{0}:{1}'.format(name, value)))
    calendar.append('END:VEVENT')


_VEVENT_BLOCK = re.compile(r'^BEGIN:VEVENT\r?\n.*?^END:VEVENT\r?$', re.M | re.S)
_UID_IN_BLOCK = re.compile(r'^UID:(.+)\r?$', re.M)
_UID_LINE = re.compile(r'^UID:.+\r?$', re.M)
_MEDIA_IN_UID = re.compile(r'^(?P<id>-?\d+)-')
_DTSTART_ANY = re.compile(r'^DTSTART[^:]*:(?P<v>.+)\r?$', re.M)
_SUMMARY_ANY = re.compile(r'^SUMMARY:(?P<v>.+)\r?$', re.M)
_OLD_SUMMARY = re.compile(
    r'^SUMMARY:(?P<title>.+?)(?P<episode> - Episode \d+| \(date TBA\))? '
    r'\[(?P<providers>[^\]]+)\]\r?$', re.M)


def export_anime_calendar(path, excluded):
    """Export-AnimeCalendar."""
    if S.args.IncludePastEpisodes:
        retention_start = datetime.min.replace(tzinfo=UTC)
    else:
        today = datetime.now(UTC)
        retention_start = datetime(today.year, today.month, today.day,
                                   tzinfo=UTC) - timedelta(days=1)
    selected = {str(media_id): True for media_id in as_list(S.db['Selections'])}

    calendar_events = []
    for season in S.db['Seasons'].values():
        for entry in as_list(season.get('Events')):
            media_id = str(entry.get('MediaId'))
            if media_id not in selected:
                continue
            if excluded and media_id in excluded:
                continue
            show = ci_get(S.db['Shows'], media_id)
            if show is None:
                continue
            instant = get_event_instant(entry)
            if not instant or instant < retention_start:
                continue
            calendar_events.extend(new_calendar_event(entry, show))

    # Fail closed if an excluded media id survived the filter above.
    if excluded:
        leaked = [event for event in calendar_events if str(event.MediaId) in excluded]
        if leaked:
            raise RuntimeError('Exclusion integrity check failed: {0} excluded events remain.'
                               .format(len(leaked)))

    calendar = [
        'BEGIN:VCALENDAR',
        'VERSION:2.0',
        'PRODID:-//anime-ics//EN',
        'CALSCALE:GREGORIAN',
        'METHOD:PUBLISH',
        convert_to_ics_line('X-WR-CALNAME:Anime Calendar'),
        # Subscription clients use these to decide how often to re-poll the file.
        'REFRESH-INTERVAL;VALUE=DURATION:PT12H',
        'X-PUBLISHED-TTL:PT12H',
    ]

    stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')
    uids = set()
    event_keys = set()

    for entry in sorted(calendar_events, key=lambda event: (event.Start, event.Summary or '')):
        if entry.Uid in uids:
            continue
        uids.add(entry.Uid)
        start_key = (entry.Start.strftime('%Y%m%d') if entry.AllDay
                     else entry.Start.strftime('%Y%m%dT%H%M%SZ'))
        event_keys.add('{0}|{1}'.format(start_key, convert_to_ics_text(entry.Summary)))
        add_ics_event(calendar, entry, stamp)

    # Merge previously written calendars. Only the output file is merged unless
    # -MergeFrom names more; the old script silently absorbed every anime-*.ics
    # it could find on the Desktop.
    merge_paths = []
    seen_paths = set()

    def remember(candidate):
        full = os.path.abspath(candidate)
        marker = os.path.normcase(full)
        if marker in seen_paths:
            return
        seen_paths.add(marker)
        merge_paths.append(full)

    if os.path.exists(path):
        remember(path)
    for pattern in as_list(S.args.MergeFrom):
        if not pattern:
            continue
        for resolved in sorted(globmodule.glob(pattern)):
            if os.path.isfile(resolved):
                remember(resolved)

    merged_count = 0
    for existing_path in merge_paths:
        try:
            with open(existing_path, 'r', encoding='utf-8-sig', errors='replace') as handle:
                raw = handle.read()
        except OSError:
            continue
        for match in _VEVENT_BLOCK.finditer(raw):
            block = match.group(0)
            uid_match = _UID_IN_BLOCK.search(block)
            if not uid_match:
                continue
            uid = get_canonical_event_uid(uid=uid_match.group(1))
            media_match = _MEDIA_IN_UID.match(uid)
            if media_match:
                media_id = media_match.group('id')
                retain_existing = media_id in S.db.get('RetainedCalendarShows', [])
                if not retain_existing and excluded and media_id in excluded:
                    continue
                if not retain_existing and ci_contains(S.db['Shows'], media_id):
                    show = ci_get(S.db['Shows'], media_id)
                    if media_id not in selected or not test_show_available(show):
                        continue
                    slug = uid.split('@')[0].rsplit('-', 1)[-1]
                    enabled_slugs = [re.sub(r'[^A-Za-z0-9]', '', name) for name in S.db['EnabledProviders']]
                    if slug == 'all':
                        if any(not in_ci(name, S.db['EnabledProviders']) for name in show.get('Providers', {})):
                            continue
                    elif not in_ci(slug, enabled_slugs):
                        continue
            existing_start = get_event_start_from_ics_block(block)
            if existing_start and existing_start < retention_start:
                continue
            start_match = _DTSTART_ANY.search(block)
            summary_match = _SUMMARY_ANY.search(block)
            event_key = '{0}|{1}'.format(
                start_match.group('v').strip() if start_match else '',
                summary_match.group('v').strip() if summary_match else '')
            if uid in uids or event_key in event_keys:
                continue
            uids.add(uid)
            event_keys.add(event_key)
            block = _UID_LINE.sub(lambda _m: 'UID:{0}'.format(uid), block)
            block = _OLD_SUMMARY.sub(
                lambda m: 'SUMMARY:{0} ({1}){2}'.format(
                    m.group('title'), m.group('providers'), m.group('episode') or ''),
                block)
            for line in re.split(r'\r?\n', block):
                if line:
                    calendar.append(line.rstrip('\r'))
            merged_count += 1
    calendar.append('END:VCALENDAR')

    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)
    # UTF-8 without a BOM, CRLF line endings, and a trailing CRLF.
    with open(path, 'wb') as handle:
        handle.write((CRLF.join(calendar) + CRLF).encode('utf-8'))

    return SimpleNamespace(Path=path, Total=len(uids), Generated=len(calendar_events),
                           Merged=merged_count)


def resolve_output_path(requested, fallback_directory):
    """Resolve-OutputPath."""
    if not requested:
        return os.path.join(fallback_directory, CALENDAR_FILENAME)
    full = os.path.abspath(requested)
    if os.path.splitext(full)[1].lower() == '.ics':
        return full
    return os.path.join(full, CALENDAR_FILENAME)


def sync_exclusion_state():
    """Sync-ExclusionState."""
    selected_ids = {str(media_id): True for media_id in as_list(S.db['Selections'])}
    mapping = {}
    for season in S.db['Seasons'].values():
        for media_id in get_season_show_ids(season):
            key = str(media_id)
            if key not in selected_ids and ci_contains(S.db['Shows'], key):
                mapping[key] = str((ci_get(S.db['Shows'], key) or {}).get('Title') or '')
    return mapping


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
def get_desktop_directory():
    """The equivalent of [Environment]::GetFolderPath('Desktop')."""
    if os.name == 'nt':
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r'Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders')
            try:
                value, _ = winreg.QueryValueEx(key, 'Desktop')
            finally:
                key.Close()
            value = os.path.expandvars(value)
            if value and os.path.isdir(value):
                return value
        except Exception:
            pass
    candidate = os.path.join(os.path.expanduser('~'), 'Desktop')
    return candidate if os.path.isdir(candidate) else ''


def list_providers_report():
    write_host('\n  Discovered streaming providers', 'Cyan')
    for provider in sort_ci(list(S.db['ProviderDirectory'].values()),
                            key=lambda entry: entry.get('Name')):
        write_host('   {0}'.format(provider.get('Name')))
    write_host('')
    save_anime_database()


def list_cache_report():
    write_host('')
    write_host('  Database: {0}'.format(S.db_path), 'Cyan')
    if os.path.exists(S.db_path):
        write_host('  Size    : {0:,} bytes'.format(os.path.getsize(S.db_path)))
    else:
        write_host('  Size    : not created yet')
    write_host('  Shows   : {0}   Selected: {1}'.format(
        len(S.db['Shows']), len(as_list(S.db['Selections']))))
    write_host('  Output  : {0}'.format(S.db['OutputDirectory']))
    write_host('')
    if not S.db['Seasons']:
        write_host('  No cached seasons.')
    for key, cached in S.db['Seasons'].items():
        write_host('  {0:<12} episodes {1:>5}   aired-included {2:<5}   complete {3:<5}   '
                   'updated {4}'.format(key, len(as_list(cached.get('Events'))),
                                        str(bool(cached.get('IncludesAired'))),
                                        str(bool(cached.get('Complete'))),
                                        cached.get('UpdatedUtc')))
    write_host('')


def clear_cache_action():
    S.db['Seasons'] = {}
    S.db['Shows'] = {}
    S.db['ShowAliases'] = {}
    S.db['PendingUpdates'] = []
    if S.args.Force:
        # KnownShows goes with Selections: keeping it while dropping selections
        # would leave every re-scraped show permanently deselected.
        S.db['Selections'] = []
        S.db['RetainedCalendarShows'] = []
        S.db['AppliedSelections'] = []
        S.db['KnownShows'] = []
        write_host('Cached seasons and saved selections cleared.', 'Yellow')
    else:
        write_host('Cached seasons cleared. Selections kept (use -Force to clear those too).',
                   'Yellow')
    set_database_dirty()
    save_anime_database()


def startup_update_check(current_season):
    """The startup re-scrape that looks for new or rescheduled next-season shows.

    This used to relaunch the script in a child process; it runs inline against
    the same database.
    """
    upcoming = get_next_anime_season(current_season)
    cached_next = ci_get(S.db['Seasons'], upcoming.key())
    if cached_next is None:
        return
    incomplete = (not bool(cached_next.get('Complete'))
                  or any(entry.get('AllDay') for entry in as_list(cached_next.get('Events'))))
    if not incomplete:
        return
    before = {}
    for entry in as_list(cached_next.get('Events')):
        instant = get_event_instant(entry)
        media_id = str(entry.get('MediaId'))
        if media_id not in before or (instant and before[media_id]
                                      and instant < before[media_id]):
            before[media_id] = instant
    write_host('Checking {0} for updates...'.format(upcoming.label()), 'DarkGray')
    try:
        refreshed = update_season_cache(upcoming.Season, upcoming.Year,
                                        include_aired=bool(S.args.IncludePastEpisodes))
        after = {}
        for entry in as_list(refreshed.get('Events')):
            instant = get_event_instant(entry)
            media_id = str(entry.get('MediaId'))
            if media_id not in after or (instant and after[media_id]
                                         and instant < after[media_id]):
                after[media_id] = instant
        updates = as_list(S.db['PendingUpdates'])
        for media_id, instant in after.items():
            if media_id not in before:
                reason = 'New show'
            elif before[media_id] != instant:
                reason = 'First release updated'
            else:
                reason = None
            if not reason:
                continue
            if any(str((item or {}).get('MediaId')) == str(media_id) for item in updates):
                continue
            record = ci_get(S.db['Shows'], media_id) or {}
            updates.append({
                'MediaId': str(media_id),
                'Title': str(record.get('Title') or ''),
                'Reason': reason,
                'NewStart': round_trip_stamp(instant) if instant else None,
            })
        S.db['PendingUpdates'] = updates
        set_database_dirty()
        save_anime_database()
    except Exception as error:
        write_warning('Update check failed: {0}'.format(error))


# ---------------------------------------------------------------------------
# Main flow
#
# A state machine rather than the old pattern of relaunching the script by path
# to move between menus. Navigation no longer nests script instances, reloads
# the database, or depends on the script's own path.
# ---------------------------------------------------------------------------
def run(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.Providers = split_list(args.Providers) if args.Providers is not None else None
    args.MergeFrom = split_list(args.MergeFrom) if args.MergeFrom is not None else None
    S.args = args
    S.bound = bound_parameters(argv)
    S.category_filter = args.CategoryFilter
    enable_console_features()

    script_root = os.path.dirname(os.path.abspath(__file__))
    desktop = get_desktop_directory() or script_root
    S.db_path = (os.path.abspath(args.DatabasePath) if args.DatabasePath
                 else os.path.join(script_root, 'anime-ics-db.json'))
    S.db = import_anime_database(S.db_path, desktop)
    resolve_duplicate_shows()

    if 'Providers' in S.bound:
        S.db['EnabledProviders'] = list(args.Providers or [])
        set_database_dirty()
    S.providers = list(S.db['EnabledProviders'])

    for provider in list(S.db['ProviderDirectory'].values()):
        main_url = get_provider_main_url(provider.get('Url'))
        if provider.get('Url') != main_url:
            provider['Url'] = main_url
            set_database_dirty()
    for show in S.db['Shows'].values():
        clean = compress_synopsis_whitespace(remove_synopsis_source_prefix(show.get('Synopsis')))
        if clean != str(show.get('Synopsis') or ''):
            show['Synopsis'] = clean
            set_database_dirty()
        for name, url in list((show.get('Providers') or {}).items()):
            register_provider(name, str(url))
    save_anime_database()

    if args.ListProviders:
        list_providers_report()
        return 0

    if args.ListCache:
        list_cache_report()
        return 0

    if args.ClearCache:
        clear_cache_action()
        return 0

    if args.NextSeason and ('Season' in S.bound or 'Year' in S.bound):
        raise SystemExit('NextSeason cannot be combined with an explicit Season or Year.')

    current_season = get_anime_season()
    target_season = current_season
    if args.NextSeason:
        target_season = get_next_anime_season(current_season)
    if args.Season:
        target_season = Season(args.Season, args.Year if args.Year else target_season.Year)
    elif args.Year:
        target_season = Season(target_season.Season, args.Year)

    interactive = (not args.NoMenu) and test_console_input()

    if interactive and not args.SkipStartupUpdateCheck:
        startup_update_check(current_season)

    season_data = None
    shows = []
    excluded = {}
    export_path = None
    refresh_used = False

    state = 'MAIN' if interactive else 'LOAD'
    exit_requested = False

    while not exit_requested:
        if state == 'MAIN':
            visible_updates = get_visible_pending_updates()
            items = [MenuItem('Create or update a calendar', 'LOAD')]
            items.append(MenuItem('Providers ({0} enabled)'.format(
                len(as_list(S.db['EnabledProviders']))), 'PROVIDERS'))
            if visible_updates:
                items.append(MenuItem('Review updated shows ({0})'.format(
                    len(visible_updates)), 'UPDATES'))
            if S.db.get('PendingExport'):
                items.append(MenuItem('Update ICS with modifications'
                                      if has_show_selection_modifications()
                                      else 'Export pending changes', 'EXPORT'))
            items.append(MenuItem('Airing this week', 'WEEK'))
            items.append(MenuItem('Quit', 'QUIT'))
            footer = ['Data Scraped (Total/Session): {} / {}'.format(
                format_data_size(S.db.get('ScrapeBytesTotal', 0)),
                format_data_size(sum(S.download_bytes.values())))]
            choice = show_key_menu('Anime Calendar', items, footer=footer)
            if choice is None or choice == 'QUIT':
                state = 'QUIT'
            elif choice == 'LOAD':
                # The season is only asked for when the command line did not
                # already pin one down.
                pinned = args.NextSeason or 'Season' in S.bound or 'Year' in S.bound
                state = 'LOAD' if pinned else 'SEASONPICK'
            else:
                state = choice

        elif state == 'PROVIDERS':
            answer = show_providers_menu(current_season)
            state = answer if answer in ('QUIT', 'EXPORT') else 'MAIN'

        elif state == 'WEEK':
            answer = show_week_airings()
            state = answer if answer in ('QUIT', 'EXPORT') else 'MAIN'

        elif state == 'UPDATES':
            answer = show_pending_updates()
            state = answer if answer in ('QUIT', 'EXPORT') else 'MAIN'

        elif state == 'SEASONPICK':
            picked = show_key_menu('Select anime season', get_season_menu_items(current_season),
                                   back=True)
            if picked == 'QUIT':
                state = 'QUIT'
            elif picked == 'EXPORT':
                state = 'EXPORT'
            elif picked is None:
                state = 'MAIN'
            else:
                target_season = picked
                state = 'GENRE'

        elif state == 'GENRE':
            picked = show_genre_menu('Select genre - {0}'.format(target_season.label()))
            if picked == 'QUIT':
                state = 'QUIT'
            elif picked == 'EXPORT':
                state = 'EXPORT'
            elif picked is None:
                state = 'SEASONPICK'
            else:
                S.category_filter = picked
                state = 'LOAD'

        elif state == 'LOAD':
            try:
                # -Refresh applies to the first load only. Without this, every
                # trip back through the genre menu would re-scrape AniList.
                force_refresh = bool(args.Refresh) and not refresh_used
                season_data = get_season_data(target_season.Season, target_season.Year,
                                              force_refresh=force_refresh)
                refresh_used = True
            except Exception as error:
                write_warning('Could not load {0}: {1}'.format(target_season.label(), error))
                if not interactive:
                    raise
                write_host('Press any key to return to the menu.', 'DarkGray')
                read_menu_key()
                state = 'MAIN'
                continue
            # Shows are not added to the calendar by default. A newly discovered
            # title is recorded in KnownShows but left out of Selections, so it
            # appears under "Shows not added" until you choose it. KnownShows is
            # still what separates "never seen" from "deliberately removed".
            known = {str(media_id): True for media_id in as_list(S.db['KnownShows'])}
            changed = False
            for media_id in get_season_show_ids(season_data):
                if str(media_id) in known:
                    continue
                known[str(media_id)] = True
                changed = True
            if changed:
                S.db['KnownShows'] = sorted(known.keys(), key=ps_sort_key)
                set_database_dirty()
                save_anime_database()
            excluded = sync_exclusion_state()
            shows = get_season_shows(season_data, S.category_filter)
            state = 'MANAGE' if interactive else 'EXPORT'

        elif state == 'MANAGE':
            answer = show_management_menu(shows, excluded, target_season.label())
            save_anime_database()
            if answer == 'QUIT':
                state = 'QUIT'
            elif answer == 'EXPORT':
                state = 'FOLDER'
            elif answer == 'GENRE':
                state = 'GENRE'
            elif answer == 'SEASON':
                season_data = None
                state = 'SEASONPICK'
            else:
                state = 'MAIN'

        elif state == 'FOLDER':
            if args.OutputPath:
                export_path = resolve_output_path(args.OutputPath, S.db['OutputDirectory'])
                state = 'EXPORT'
                continue
            start = (S.db['OutputDirectory']
                     if S.db.get('OutputDirectory') and os.path.isdir(S.db['OutputDirectory'])
                     else desktop)
            folder = select_calendar_folder(start)
            if folder in ('QUIT', 'EXPORT'):
                state = folder
            elif folder is None:
                state = 'MANAGE'
            else:
                export_path = os.path.join(folder, CALENDAR_FILENAME)
                S.db['OutputDirectory'] = folder
                set_database_dirty()
                state = 'EXPORT'

        elif state == 'EXPORT':
            if not export_path:
                fallback = S.db.get('OutputDirectory') or desktop
                export_path = resolve_output_path(args.OutputPath, fallback)
            if os.path.splitext(export_path)[1].lower() != '.ics':
                raise SystemExit('OutputPath must end in .ics.')
            excluded = sync_exclusion_state()
            result = export_anime_calendar(export_path, excluded)
            S.db['OutputDirectory'] = os.path.dirname(export_path)
            S.db['AppliedSelections'] = list(S.db['Selections'])
            S.db['PendingExport'] = False
            set_database_dirty()
            save_anime_database()

            write_host('')
            write_host('Created {0}'.format(result.Path), 'Green')
            write_host('  {0} event(s): {1} generated, {2} merged from existing calendars. '
                       'Genre filter: {3}.'.format(result.Total, result.Generated,
                                                   result.Merged, S.category_filter), 'Green')
            if result.Total == 0:
                # Distinguish "you have not picked anything yet" from "there is
                # nothing to pick". Since shows are not added by default, the
                # first is much the more likely of the two.
                if not as_list(S.db['Selections']) and S.db['Shows']:
                    write_warning('The calendar is empty because no shows have been added. '
                                  "Shows are not added by default - choose them under 'Shows "
                                  "not added' in the menu (Space adds), then export again.")
                else:
                    write_warning('No titles currently have both a supported streaming-provider '
                                  'attribution and a published release time. Upcoming-season '
                                  'data appears as services announce their lineups; try again '
                                  'later.')
            write_warning('Airing times are AniList broadcast times; streaming releases may be '
                          'later and region dependent.')
            if S.anilist_requests > 0:
                write_verbose('AniList requests this run: {0}'.format(S.anilist_requests))

            if not interactive:
                state = 'QUIT'
                continue
            after = show_key_menu('Calendar created', [
                MenuItem('Back to add/remove shows', 'MANAGE'),
                MenuItem('Quit', 'QUIT'),
            ])
            if after == 'MANAGE':
                # Exporting straight from the main menu never loads a season, so
                # there may be nothing to manage yet.
                if not season_data:
                    state = 'SEASONPICK'
                else:
                    excluded = sync_exclusion_state()
                    shows = get_season_shows(season_data, S.category_filter)
                    state = 'MANAGE'
            else:
                state = 'QUIT'

        elif state == 'QUIT':
            exit_requested = True

        else:
            exit_requested = True

    save_anime_database()
    if interactive:
        clear_host()
        if export_path and os.path.exists(export_path):
            write_host('Calendar saved to {0}'.format(export_path), 'Green')
        else:
            write_host('No calendar changes were exported.', 'DarkGray')
    if args.MetricsPath:
        metrics = {
            'AniListRequests': S.anilist_requests,
            'Sources': S.download_bytes,
            'TotalBytes': sum(S.download_bytes.values()),
        }
        with open(args.MetricsPath, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(json.dumps(metrics, indent=2, ensure_ascii=False))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        return run(argv)
    except KeyboardInterrupt:
        # Menu toggles are already in the database; flush them rather than
        # losing the picker session to a stray Ctrl+C.
        try:
            if S.db is not None:
                save_anime_database()
        except Exception:
            pass
        write_host('')
        write_host('Cancelled.', 'DarkGray')
        return 130
    except SystemExit as error:
        if isinstance(error.code, str):
            write_warning(error.code)
            return 1
        raise
    except Exception as error:
        write_warning(str(error))
        if S.args is not None and S.args.Verbose:
            import traceback
            traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())
