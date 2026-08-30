# Anime ICS Calendar

Create a personal `.ics` calendar for seasonal anime on the streaming services
you use.

`ics_anime.py` fetches anime schedules from [AniList](https://anilist.co), adds
streaming attribution from AniList, [LiveChart](https://www.livechart.me), and
optional official lineup pages, then lets you choose exactly which shows belong
in your calendar.

It runs on Windows, macOS, and Linux with Python's standard library—there are no
packages to install.

> [!IMPORTANT]
> AniList publishes Japanese broadcast airtimes. Streaming releases may occur
> later, and availability varies by region.

## Features

- Interactive keyboard menus for seasons, providers, genres, and shows
- Current-season and next-season schedules
- Persistent provider and show selections
- Offline JSON cache for fast repeat runs
- Month-only `(date TBA)` placeholders for unannounced premiere dates
- One event per provider, or one combined event per episode
- Optional merging with existing `.ics` calendars
- RFC 5545 output with UTC times, all-day dates, stable UIDs, and event sequences
- Windows PowerShell-style and GNU-style command-line options
- Compatible database and calendar output with `ics_anime.ps1`

## Requirements

- Python 3.8 or newer
- Internet access when discovering providers or refreshing schedule data
- A terminal that supports single-key input for interactive mode

## Getting started

Download `ics_anime.py`, open a terminal in its directory, and run:

```bash
python3 ics_anime.py
```

On Windows, if Python is available through the launcher:

```powershell
py ics_anime.py
```

On the first run:

1. Open **Providers**.
2. Highlight **Update providers and shows** and press `Space`. This scans the
   current and next seasons to discover streaming services.
3. Enable your services with `Space`.
4. Return to the main menu and choose **Create or update a calendar**.
5. Select a season and genre view.
6. Open **Shows not added** and select the titles you want with `Space`.
7. Choose **Update ICS with modifications** and select an output folder.

The script creates `anime-calendar.ics`. Import that file into your calendar app,
or place it somewhere your calendar can subscribe to it.

Newly discovered shows are never selected automatically. Your choices are saved
in `anime-ics-db.json` and reused on later runs.

## Menu controls

| Key | Action |
| --- | --- |
| `Up` / `Down` | Move through a menu or list |
| `Home` / `End` | Jump to the first or last entry |
| `Page Up` / `Page Down` | Move through a show list one page at a time |
| `Space` | Select, update, or toggle a provider/show |
| `Right` / `Enter` | Select an item; `Right` opens details where available |
| `Left` | Go back |
| `/` | Filter the current show list by title |
| `C` | Clear the title filter |
| `O` | Open a provider's website from its details screen |
| `Q` | Quit, with an export prompt when changes are pending |

Provider choices are saved immediately. Show changes remain pending until you
export the calendar. Updating providers refreshes the current and next seasons;
cached links from disabled providers are removed, along with shows that no longer
have an enabled provider.

## Command-line usage

Display all options:

```bash
python3 ics_anime.py --help
```

Every long option has two spellings. For example, `-NextSeason` and
`--next-season` are equivalent.

### Examples

```bash
# Rebuild next season using saved providers and show selections
python3 ics_anime.py --next-season --no-menu

# Refresh a specific season and include episodes that already aired
python3 ics_anime.py --season SUMMER --year 2026 --refresh \
  --include-past-episodes --no-menu

# Write to a specific calendar file
python3 ics_anime.py --output-path ./calendars/anime.ics --no-menu

# Use explicit providers and produce one event per episode
python3 ics_anime.py --providers Crunchyroll "Prime Video" \
  --combine-providers --no-menu

# Merge another calendar into the output
python3 ics_anime.py --merge-from ./other-anime.ics --no-menu

# Inspect the local cache
python3 ics_anime.py --list-cache
python3 ics_anime.py --list-providers
```

`--no-menu` is intended for scheduled jobs and automation. On a fresh database,
it creates an empty calendar because no shows have been selected. Configure the
calendar interactively first, or reuse an existing database with `--database-path`.

Passing `--providers` replaces the saved enabled-provider list but does not select
shows. `--all-discovered-providers` enables and saves every provider encountered
during a refresh.

## Options

| Option | Description |
| --- | --- |
| `-OutputPath`, `--output-path PATH` | Write to a specific `.ics` file, or write `anime-calendar.ics` inside a directory. Defaults to the remembered output directory, then the Desktop. |
| `-Providers`, `--providers NAME ...` | Use the named streaming providers and replace the saved provider selection. Comma-separated names are also accepted. |
| `-Season`, `--season NAME` | Select `WINTER`, `SPRING`, `SUMMER`, or `FALL`. Defaults to the current season. |
| `-Year`, `--year YYYY` | Select a year from 2000 through 2100. Defaults to the current year. |
| `-NextSeason`, `--next-season` | Select the season immediately after the current one. Cannot be combined with an explicit season or year. |
| `-IncludePastEpisodes`, `--include-past-episodes` | Include episodes that have already aired. A future-only cache is refreshed automatically when necessary. |
| `-CombineProviders`, `--combine-providers` | Produce one event listing every matching service instead of one event per provider. |
| `-CategoryFilter`, `--category-filter FILTER` | Show `All`, `FantasyIsekaiReincarnation`, or `ExcludeFantasyIsekaiReincarnation` in the picker. This does not select titles. |
| `-ProviderOverridesPath`, `--provider-overrides-path CSV` | Read regional provider corrections from a CSV file. |
| `-LineupSourcePath`, `--lineup-source-path CSV` | Read official seasonal lineup pages from a CSV file. |
| `-MergeFrom`, `--merge-from PATH ...` | Merge additional `.ics` files or wildcard paths into the output. |
| `-Refresh`, `--refresh` | Fetch the target season again instead of using cached data. |
| `-ListProviders`, `--list-providers` | Print providers already discovered in the database, then exit. |
| `-ListCache`, `--list-cache` | Print the database path, size, selections, and cached seasons, then exit. |
| `-ClearCache`, `--clear-cache` | Remove cached seasons and shows while preserving saved selections. |
| `-Force`, `--force` | With `--clear-cache`, also remove selections and known-show history. |
| `-AllDiscoveredProviders`, `--all-discovered-providers` | Enable every provider discovered while scraping. |
| `-MetricsPath`, `--metrics-path PATH` | Write AniList request counts and downloaded-byte totals to JSON. |
| `-DatabasePath`, `--database-path PATH` | Use another database file instead of `anime-ics-db.json` beside the script. |
| `-NoMenu`, `--no-menu` | Run without interactive menus. |
| `-SkipStartupUpdateCheck`, `--skip-startup-update-check` | Skip the interactive startup refresh for an incomplete next-season cache. |
| `-Verbose`, `--verbose` | Print request and diagnostic details. |
| `-NoColor`, `--no-color` | Disable ANSI colors. The standard `NO_COLOR` environment variable is also respected. |

## Optional CSV inputs

### Provider overrides

Use `--provider-overrides-path` to assign a provider and watch URL to a specific
AniList title. The provider must also be enabled.

```csv
AniListId,Provider,Url
21,Crunchyroll,https://www.crunchyroll.com/series/example
```

### Seasonal lineup pages

Use `--lineup-source-path` to add official provider pages for a season. When a
title appears on a matching page but AniList and LiveChart provide no streaming
link, the lineup page is used as its provider URL.

```csv
Season,Year,Provider,Url
FALL,2026,HIDIVE,https://www.hidive.com/fall-2026
```

LiveChart is queried automatically and does not need to appear in this file.

## Cache behavior

The default `anime-ics-db.json` stores:

- cached seasons and episode schedules;
- show metadata and provider links;
- enabled providers and selected shows;
- the last output directory;
- pending next-season updates;
- cumulative download totals.

The database schema is version 4 and is compatible with `ics_anime.ps1`. Both
scripts can share the same database. An unreadable database or a file with another
schema version is ignored and rebuilt rather than partially migrated.

`--refresh` applies to the first season loaded during the run. In interactive mode,
the script also rechecks an incomplete cached next season at startup unless
`--skip-startup-update-check` is supplied.

## Calendar behavior

- Timed events are written in UTC; calendar applications convert them locally.
- All-day releases use date values and do not shift across time zones.
- Month-only premieres appear on the first of the month as `(date TBA)` placeholders.
- Shows with no announced month remain selectable but generate no event yet.
- Rescheduled events receive a higher `SEQUENCE`, helping subscribers apply changes.
- Events contain a synopsis, AniList URL, provider names, and available watch links.
- Events are transparent, so they do not mark you as busy.
- Output is UTF-8 without a byte-order mark and uses 75-octet line folding.
- Twelve-hour refresh hints are included for calendar subscription clients.

Without `--include-past-episodes`, events older than yesterday in UTC are omitted.
The existing output file is always merged back into the new calendar so unrelated
events survive. Extra calendars are merged only when passed to `--merge-from`.
Duplicate UIDs and matching start/title pairs are skipped.

## Data sources and privacy

- AniList supplies schedules, titles, metadata, and most synopses.
- LiveChart can supplement provider links, schedules, synopses, and missing titles.
- Optional lineup pages provide fallback streaming attribution.
- AniList rate limits and transient server errors are retried with backoff.

The script does not upload your selections. It writes only its JSON database, the
requested `.ics` file, and an optional metrics JSON file.

## Troubleshooting

**The generated calendar is empty**

Open the interactive picker and add shows under **Shows not added**. Also confirm
that at least one provider is enabled. Upcoming shows without a published date do
not produce an event until one becomes available.

**The script uses cached information**

Run with `--refresh`, or inspect and clear the cache with `--list-cache` and
`--clear-cache`.

**Menus do not appear**

Interactive menus require a TTY. When input or output is redirected, the script
automatically follows the non-interactive path. Run it directly in a terminal.

**Colors render incorrectly**

Use `--no-color` or set the `NO_COLOR` environment variable.
