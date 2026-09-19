# Anime ICS Calendar — Python

[`ics_anime.py`](ics_anime.py) builds an `.ics` calendar from anime schedules and lets you choose which streaming providers and shows to follow. It includes an interactive keyboard interface, persistent selections, a local JSON cache, and a chronological view of selected episodes airing this week.

For the PowerShell implementation, see [README.md](README.md).

## Contents

- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Main menu](#main-menu)
- [Providers and removal confirmations](#providers-and-removal-confirmations)
- [Seasons and genres](#seasons-and-genres)
- [Selecting shows and exporting changes](#selecting-shows-and-exporting-changes)
- [Review updated shows](#review-updated-shows)
- [Airing this week](#airing-this-week)
- [Keyboard controls](#keyboard-controls)
- [Dates, completed shows, and duplicates](#dates-completed-shows-and-duplicates)
- [Calendar contents and merging](#calendar-contents-and-merging)
- [Cache, refreshes, and data counters](#cache-refreshes-and-data-counters)
- [Command-line reference](#command-line-reference)
- [Command examples](#command-examples)
- [Optional CSV sources](#optional-csv-sources)
- [Automation and calendar subscriptions](#automation-and-calendar-subscriptions)
- [Troubleshooting](#troubleshooting)
- [Tests](#tests)

## Requirements

- **Python 3.8 or later**, using only the standard library. There is no `pip install` step or requirements file for this script.
- A terminal with nonredirected input/output for interactive menus. Windows uses `msvcrt`; Unix-like systems use `termios` and raw key input.
- Internet access when fetching or refreshing schedules and provider information.
- Write access to the database location and output calendar folder.

The implementation includes Windows and Unix-like terminal paths. Regression checks and live terminal checks have been run on Windows; Linux/macOS interactive behavior has not been verified in this workspace.

No API credentials are required. The script uses AniList, LiveChart, and any optional lineup pages you configure. Provider availability depends on those sources and may differ by region. Episode timestamps describe published broadcast schedules; they are not a guarantee of the exact release time on each streaming service.

Check which Python interpreter you are using:

```console
python --version
```

If your system exposes Python 3 as `python3`, substitute `python3` for `python` in the commands below.

## Quick start

Download `ics_anime.py` to a writable folder, then open a terminal in that folder:

```console
python ics_anime.py
```

For a new database:

1. Open **Providers**.
2. Select **Update providers and shows** with Space. This discovers providers and caches the current and next seasons.
3. Highlight each provider you use and press Space to enable it. New databases start with all providers disabled.
4. Return to the main menu with Left Arrow.
5. Choose **Create or update a calendar**, then a season and genre filter.
6. Open **Shows not added** and press Space on the shows you want. `[x]` means selected; `[ ]` means not selected.
7. Return to the management menu and choose **Update ICS with modifications** or **Export calendar**.
8. Choose an output folder. The default calendar filename is `anime-calendar.ics`.

Selecting a provider makes its shows available to browse; it does **not** select all of those shows for export. New discoveries are never automatically added to your calendar.

The default database is `anime-ics-db.json` beside the script. The output directory is remembered in that database. When no output directory has been saved, the Desktop is the normal fallback.

## Main menu

| Option | Function |
| --- | --- |
| Create or update a calendar | Opens season selection, genre selection, and show management. Explicit command-line season options can bypass season selection. |
| Providers | Enables/disables services, refreshes cached provider/show information, and opens provider details. |
| Review updated shows | Appears when eligible update notifications are available. Shows their count. |
| Update ICS with modifications / Export pending changes | Appears when there are changes waiting to be exported. |
| Airing this week | Opens a local-time schedule of selected shows. It is positioned above Quit. |
| Quit | Exits. The Q shortcut offers an export/back choice when changes are pending; the explicit Quit menu item exits without exporting. |

Below Quit is a nonselectable footer:

```text
Data Scraped (Total/Session): 12.34 MB / 0.56 MB
```

**Total** is the accumulated amount recorded in this database. **Session** covers the current process only. Both display in MB, calculated as bytes divided by 1,048,576. These are decoded response-content tallies, not measurements of every byte transferred over the network.

## Providers and removal confirmations

Provider names are discovered from streaming links and normalized where recognized. Common services include Crunchyroll, HIDIVE, Prime Video, Netflix, and Disney+. The directory can grow as sources report additional services.

Enabling a provider immediately reveals its cached shows. Disabling it immediately removes shows that no longer have an enabled provider from browsing lists. Provider attribution stays in the cache, so ordinary toggles do not need another scrape. Older caches that discarded disabled-provider information need a one-time refresh when enabling a provider.

**Update providers and shows** refreshes the current and next seasons plus seasons already present in the cache. Merely displaying a future season in the season menu does not fetch it.

### Removing a provider with selected shows

Two separate decisions are presented:

1. **Remove the provider?** A paginated list shows its selected titles. No keeps the provider enabled. Yes proceeds to the calendar question.
2. **Also remove its shows from the ICS?**

| Answer to the second question | Result |
| --- | --- |
| No — keep existing ICS entries | The provider is disabled. Existing entries for shows losing their last enabled provider are preserved during subsequent merges, subject to the calendar's normal date-retention rules. This does not generate new episodes for those deselected shows. |
| Yes — remove and update ICS now | The provider is disabled and the script exports the calendar immediately, applying **all** pending calendar changes. |

Shows carried by another enabled provider remain selected. If a provider is later re-enabled, retained shows available through it are restored to the selected set.

Right Arrow on a provider opens its details, including a website and up to five popular currently airing titles from the current cached season. O opens its website in the default browser.

## Seasons and genres

The season menu offers **Current**, **Next**, and **Future**. Calendar seasons are:

| Season | Months |
| --- | --- |
| WINTER | January–March |
| SPRING | April–June |
| SUMMER | July–September |
| FALL | October–December |

The computer's date supplies the base current season. When the cached current season is confirmed finished, the menu advances its labels. For example, Summer 2026, Fall 2026, and Winter 2027 can become Current Fall 2026, Next Winter 2027, and Future Spring 2027. Unknown completion dates do not by themselves establish that a season has finished.

An uncached future season is fetched after you choose it and a genre. Returning between menus normally reuses the cache. Continuing shows can appear in another season when cached episode dates place them there; matching a series title alone does not automatically select its sequel.

The genre choices are:

- **All anime**.
- **Fantasy, isekai and reincarnation**: the Fantasy genre or qualifying, non-spoiler tags for the related themes.
- **Non-fantasy / non-isekai / non-reincarnation**: the inverse group.

Genre filtering controls visibility, not selection or export scope. Choosing one genre does not remove previously selected shows in another genre. Likewise, the season being browsed controls what is loaded/displayed; an export can include selected shows across cached seasons.

## Selecting shows and exporting changes

The management menu contains **Shows in the calendar**, **Shows not added**, and an export action.

List membership and counts reflect the **last successful export**. Checkboxes reflect your **current pending selections**. A newly checked title can therefore remain in Shows not added until you export; this lets you revise multiple choices before applying them.

- Space toggles a show.
- `/` opens a case-insensitive title filter; C clears it.
- Right Arrow opens the title, selected status, enabled providers, next known release, and synopsis.
- Left Arrow returns to management, genre selection, or the preceding screen.

Choices are kept in the database and flushed during normal navigation or exit. Checking a box does not itself rewrite the ICS. Export applies those choices, records the applied selection set, and clears the pending-export flag. The provider-removal **Yes** action is an exception: it explicitly requests immediate export.

## Review updated shows

At interactive startup, the script can recheck an **already cached next season** when its schedule is incomplete or contains all-day placeholders. The comparison looks for new dated shows and changes to a show's earliest known release. It is not a notification feed for every episode-level change.

Review entries show a title/provider label and update reason. Space selects a show and immediately removes it from review. Currently selected and previously selected IDs are suppressed using persistent selection history, including known duplicate aliases. Disabled-provider and completed-show entries are not displayed.

D dismisses the current pending notification collection. A later update can produce a new notification for a dismissed, never-selected show. Previously selected shows remain suppressed by history. Right Arrow opens details.

Old databases can seed history from current and last-exported selections. They cannot reconstruct removed selections that were never recorded.

Navigation reuses a snapshot of the review list; it does not rescan every cached episode on each arrow press. Completion is rechecked when the menu opens or its local snapshot crosses a UTC date boundary.

## Airing this week

This is a read-only view of **current selections**, including changes that have not yet been exported. It reads cached schedules; opening it or pressing R does not scrape the websites.

The window covers **today and the next six calendar days in the computer's timezone**. Timed episodes earlier than the current instant are omitted. Rows are grouped by weekday and date and sorted chronologically, with episode numbers and provider labels.

```text
Saturday, September 19
  9:00 PM  Example Show (Crunchyroll) - Episode 11
  TBA  Another Show (HIDIVE)
Sunday, September 20
  1:00 AM  Example Show (Crunchyroll) - Episode 12
```

- Each timestamp is converted to local time, including daylight-saving transitions.
- A confirmed date without an airtime stays on that date and displays **TBA**, after timed entries for the day.
- A completely unknown date or month-only placeholder cannot be assigned to a day and is omitted.
- Duplicate copies of the same show/episode are collapsed.
- Long lists scroll; a day heading is repeated when its entries continue beyond the visible page.
- R rebuilds the view from the current cache and clock. Use a scrape/refresh action first if the cached schedule is stale.

## Keyboard controls

| Screen | Controls |
| --- | --- |
| Standard choice menus | Up/Down move; Home/End jump; Right/Enter/Space choose; Left returns where available; Q requests quit. |
| Provider list | Up/Down move; Space toggles or runs Update; Right opens details; Left returns; Q requests quit. |
| Provider details | Up/Down select a title; Right opens show details; O opens website; Left returns. |
| Show lists | Up/Down move; Page Up/Down page; Home/End jump; Space toggles; Right opens details; `/` filters; C clears filter; Left returns; Q requests quit. |
| Review updated shows | Up/Down move; Space selects; Right opens details; D dismisses notifications; Left returns; Q requests quit. |
| Airing this week | Up/Down scroll; Page Up/Down page; Home/End jump; R refreshes the cached view; Left/Escape return; Q requests quit. |
| Quit prompt | E exports pending changes; Q quits without exporting; Left returns. |

On supported consoles, cursor movement repaints only changed rows. Paging, resizing, and returning from another screen trigger full redraws. Long rows are clipped to avoid wrapping. Consoles without usable cursor positioning fall back to ordinary redraws.

## Dates, completed shows, and duplicates

Precise timed episodes are stored in UTC. Date-only releases use calendar dates. A month-only premiere is exported as an all-day placeholder on the month's first day, with **(date TBA)** in the summary. A title with no date can still be selected, but it cannot generate an event until a date is available.

Completion checks consider cached future airings, a confirmed final episode, a complete end date, and the source's FINISHED status. A known final date must be past before the date-based check hides the show. Missing dates, invalid strings, and old cached `{}` end-date values are treated as unknown rather than crashing the picker. Hiding a completed title does not erase its saved selection or all historical calendar entries.

Duplicate reconciliation links a negative-ID LiveChart fallback to a positive-ID AniList record when:

1. Their normalized titles match, including any season number.
2. They share a provider.
3. Exactly one AniList candidate matches.

Case, repeated whitespace, and Unicode compatibility differences are normalized. Different season numbers, provider-disjoint records, and ambiguous AniList matches remain separate. This is deliberately conservative, not fuzzy matching of arbitrary similar titles.

Reconciliation runs on startup and after scraping. It remaps season references, selections, selection history, and existing-calendar IDs, preferring the AniList record while preserving additional provider information. It works across cached seasons, including an AniList title listed in Fall and its duplicate LiveChart fallback listed in Winter.

## Calendar contents and merging

Generated events include a stable UID, summary, synopsis, provider links, source link, start/end information, and sequence metadata. Synopsis resolution can walk prequel relationships to find a fuller series description when a sequel description is sparse.

By default, one event is generated per episode **per enabled provider**. `--combine-providers` creates one event per episode listing its enabled services. Multiple providers are intentional calendar entries; they are separate from duplicate source records.

The existing output ICS is merged automatically. `--merge-from` adds explicitly named files or wildcard matches. Other nearby ICS files are not scanned automatically. Known deselections and removed-provider rules apply during merging, with the explicit retained-entry exception described above.

Without `--include-past-episodes`, the implementation's retention cutoff is the start of the **previous UTC day**, allowing a short grace period around local-day boundaries. This is different from the weekly view, which excludes timed episodes that have already aired. With `--include-past-episodes`, cached past episodes and eligible older merged events can be kept. Re-scraping cannot recover history that the upstream source no longer supplies.

## Cache, refreshes, and data counters

The JSON database is written through a temporary file and replaced after serialization. This reduces the chance of an interrupted write leaving a partially written database.

The version-4 database stores provider preferences, show metadata, seasonal events, selected/applied IDs, selection history, pending updates, retained calendar IDs, duplicate aliases, the output directory, and scrape totals. It is compatible with the current [PowerShell script](ics_anime.ps1).

Run options such as `--combine-providers`, `--include-past-episodes`, explicit season/year, genre filter, and custom CSV paths are not saved as a reusable command configuration. Supply them again when needed, including in scheduled commands. The cached seasons remain available even when you start a later run without those season options.

Use only one process at a time with a shared database/output: neither implementation provides multi-process locking. A separate `--database-path` creates an independent profile.

Network access can occur when:

- A requested season is not cached.
- You supply `--refresh` (applied to the first season load in that run).
- You use the provider Update action, which also refreshes previously cached seasons.
- The interactive next-season startup update check runs.
- An older cache needs provider-data migration or lacks past airings requested by `--include-past-episodes`.

`--skip-startup-update-check` suppresses only the startup check. It is not a guarantee that every other action will stay offline.

`--metrics-path` writes a session JSON report with `AniListRequests`, `Sources`, and `TotalBytes` when a normal run completes. Metric byte values are raw integers; only the menu formats them as MB. Report-only actions that return early, such as listing or clearing the cache, do not run the final metrics-writing step.

`--clear-cache` removes cached shows, seasons, aliases, and pending notifications while keeping selections by default. Adding `--force` also clears selected/applied IDs, known-show IDs, and retained-calendar IDs. It does **not** reset every preference: provider settings, scrape totals, and selection history remain. Neither command deletes the output ICS. For a completely separate starting state, use a new database path and output path.

## Command-line reference

The Python script accepts PowerShell-style names such as `-OutputPath` and Python-style names such as `--output-path`. Use the exact spelling shown: the option parser is case-sensitive and disables abbreviated options.

| Python option | PowerShell-style alias | Purpose |
| --- | --- | --- |
| `--output-path PATH` | `-OutputPath` | Output `.ics` file or directory; a directory receives `anime-calendar.ics`. |
| `--providers NAME [NAME ...]` | `-Providers` | Sets and saves enabled providers. Does not select shows. Accepts separate arguments or comma-separated names. |
| `--season NAME` | `-Season` | `WINTER`, `SPRING`, `SUMMER`, or `FALL`; season values are converted to uppercase. |
| `--year YYYY` | `-Year` | Year from 2000 through 2100. |
| `--next-season` | `-NextSeason` | Loads the season after the computer-date-derived current season. Cannot be combined with explicit season/year options. |
| `--include-past-episodes` | `-IncludePastEpisodes` | Requests past schedules and retains eligible older events. |
| `--combine-providers` | `-CombineProviders` | One event per episode listing its enabled services. |
| `--category-filter VALUE` | `-CategoryFilter` | `All` (default), `FantasyIsekaiReincarnation`, or `ExcludeFantasyIsekaiReincarnation`. A browsing filter. |
| `--provider-overrides-path CSV` | `-ProviderOverridesPath` | Adds/replaces provider attribution using an override CSV. |
| `--lineup-source-path CSV` | `-LineupSourcePath` | Adds seasonal provider lineup pages. |
| `--merge-from PATH [PATH ...]` | `-MergeFrom` | Extra ICS files or wildcard patterns to merge. |
| `--refresh` | `-Refresh` | Forces the first requested season load to scrape again. |
| `--list-providers` | `-ListProviders` | Prints the cached provider directory and exits; does not discover providers by itself. |
| `--list-cache` | `-ListCache` | Prints database location, size, counts, and seasonal cache status; exits. |
| `--clear-cache` | `-ClearCache` | Clears scraped cache content and exits. |
| `--force` | `-Force` | With cache clearing, also clears the selection-related fields described above. |
| `--all-discovered-providers` | `-AllDiscoveredProviders` | Enables discovered providers when a scrape runs. Use with `--refresh` for an already cached season. Does not select shows. |
| `--metrics-path JSON` | `-MetricsPath` | End-of-run session metrics destination. |
| `--database-path JSON` | `-DatabasePath` | Overrides the database path beside the script. |
| `--no-menu` | `-NoMenu` | Uses saved selections and supplied options without prompts. A new database has no selected shows. |
| `--skip-startup-update-check` | `-SkipStartupUpdateCheck` | Skips the interactive next-season update check. |
| `--verbose` | `-Verbose` | Shows request diagnostics and a traceback for an unhandled error. |
| `--no-color` | `-NoColor` | Disables ANSI colors. Does not disable cursor-positioned rendering on capable terminals. |
| `-h`, `--help` | None | Prints the parser's usage/options and exits. |

The `NO_COLOR` environment variable also disables color output. The `>` marker still identifies the highlighted item.

```console
python ics_anime.py --help
```

Normal completion returns exit code `0`; handled execution failures return `1`; Ctrl+C returns `130` after attempting to save pending database changes. Argument-parsing errors use argparse's normal exit status `2`.

Flags such as `--refresh` are standalone switches. Do not use PowerShell switch syntax such as `-Refresh:$true` with this Python program.

## Command examples

Open normally, suppressing only the automatic startup update:

```console
python ics_anime.py --skip-startup-update-check
```

Browse an explicitly chosen season; the year below is an example, not a fixed default:

```console
python ics_anime.py --season WINTER --year 2027 --category-filter FantasyIsekaiReincarnation
```

Refresh and export saved selections without menus:

```console
python ics_anime.py --no-menu --refresh --output-path ./anime-calendar.ics
```

Set providers and combine their events. Quote provider names containing spaces:

```console
python ics_anime.py --providers Crunchyroll "Prime Video" --combine-providers --refresh
```

A single comma-separated argument is also accepted:

```console
python ics_anime.py --providers "Crunchyroll,Prime Video" --combine-providers --refresh
```

Keep available past airings and write metrics:

```console
python ics_anime.py --no-menu --include-past-episodes --refresh --metrics-path ./metrics.json
```

Use an independent profile and merge extra calendars:

```console
python ics_anime.py --database-path ./profile-two.json --output-path ./profile-two.ics --merge-from "./extra/*.ics"
```

Quoting a wildcard lets the script expand it consistently rather than relying on the shell. Provider names and merge-file arguments are split on commas, so a literal comma inside a name/path cannot be represented through those list options.

The equivalent PowerShell-style aliases work when passed as normal Python arguments:

```console
python ics_anime.py -NextSeason -NoMenu -Refresh
```

Inspect or clear the cache:

```console
python ics_anime.py --list-providers
python ics_anime.py --list-cache
python ics_anime.py --clear-cache
python ics_anime.py --clear-cache --force
```

The final command also clears current/applied selections, known-show IDs, and retained-calendar IDs; it is not a reset of every database preference or history field.

## Optional CSV sources

Provider overrides use these column names:

```csv
AniListId,Provider,Url
123456,Crunchyroll,https://example.com/watch/example-show
```

Replace the example ID and URL with real values. Use one row per AniList ID: the current override lookup retains the last row for a repeated ID. Overrides add or replace the specified provider link; they do not remove every other discovered provider.

Seasonal lineup sources use:

```csv
Season,Year,Provider,Url
WINTER,2027,Crunchyroll,https://example.com/winter-lineup
```

Use uppercase season names. Matching rows supply pages whose text is checked for show titles. One provider/page mapping is used per season; repeated matching provider rows replace earlier ones. LiveChart is included automatically.

```console
python ics_anime.py --provider-overrides-path ./overrides.csv --lineup-source-path ./lineups.csv --refresh
```

## Automation and calendar subscriptions

First select shows interactively. Then run `--no-menu --refresh` from Windows Task Scheduler, cron, or another scheduler to update the file using saved choices. Use absolute interpreter, script, database, and output paths in scheduled jobs. Choose a schedule appropriate for your needs rather than repeatedly invoking a scrape in a tight loop.

Example Windows command; use your installed interpreter's actual path in the scheduler:

```text
python.exe "C:\AnimeCalendar\ics_anime.py" --no-menu --refresh --database-path "C:\AnimeCalendar\anime-ics-db.json" --output-path "C:\AnimeCalendar\anime-calendar.ics"
```

Example Unix-like command:

```sh
/usr/bin/python3 /home/you/anime-calendar/ics_anime.py --no-menu --refresh --database-path /home/you/anime-calendar/anime-ics-db.json --output-path /home/you/anime-calendar/anime-calendar.ics
```

The script writes a local ICS file; it does not upload or host it. Importing a file into a calendar app normally creates a snapshot. To obtain ongoing subscription updates, publish the generated file at a stable reachable URL and subscribe using a calendar application that supports ICS feeds. Hosting, scheduling, and client refresh behavior are outside this script.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Empty calendar on first use | Enable providers, explicitly select shows, then export. `--no-menu` and `--all-discovered-providers` do not select titles. |
| A checked show remains in Shows not added | The list uses last-exported membership. Export pending changes to update the grouping. |
| Nothing in Airing this week | Confirm selections, enabled providers, and cached dated episodes within today plus six days. Undated/month-only premieres are excluded. R refreshes the view, not the source data. |
| Provider is missing | Run Update providers and shows. Attribution depends on streaming links and page parsing; an override CSV can add a missing link. |
| Duplicate title remains | Only unambiguous matching LiveChart/AniList records sharing a provider are reconciled. Different season numbers or ambiguous candidates remain separate. |
| Future shows lack episodes | Announcements often precede schedules. Select the title if desired and refresh later. |
| Unexpected startup network activity | Use `--skip-startup-update-check`; uncached loads and cache migrations can still require fetching. |
| Rate limits or temporary failures | AniList requests retry transient errors with backoff. Use `--verbose` for details and retry a refresh later if the source remains unavailable. |
| Arrow keys do not work | Use a normal console with nonredirected input. Unsupported hosts fall back to noninteractive operation. |
| Old date-parsing crash | Current code tolerates `{}` and invalid end dates. Update the script; clearing the cache is not needed for that known issue. |
| Menu pauses | Use the current script. Review navigation caches rows, and season completion uses an episode index and stops at the first unfinished show. A real scrape still takes network time. |
| `python` is not found or is the wrong version | Use `python3` or the installed interpreter's full path, and check `--version`. |
| Colors or cursor movement are unavailable | Use a real terminal. `--no-color`/`NO_COLOR` suppress colors; terminals without virtual-terminal support use full redraws. |

## Tests

Run the standard-library unittest suite from the repository folder:

```console
python -m unittest discover -s tests -p test_ics_anime.py -v
```

Tests use fixtures, temporary files, and mocked network/console boundaries. They cover selections, cache migration, provider confirmation choices, ICS retention/merging, duplicate reconciliation, malformed dates, week-view grouping, local timezone conversion, rendering, and menu performance behavior. They do not scrape live websites or modify your real database/calendar.

The Windows input-polling regression test is platform-specific. A passing suite on Windows is not evidence that every Linux/macOS terminal has been tested.

For an in-memory review-navigation timing sample:

```console
python tests/benchmark_review.py
```

The benchmark excludes terminal output and uses synthetic shows/episodes. Its numbers are not a guarantee of performance on another machine.
