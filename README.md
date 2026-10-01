# Ontario Wait Times (CT Waits)

Ontario wait times near a postal code for CT, MRI, breast screening and every surgical procedure Ontario Health reports. Three front ends:

- `CTWaits.exe`: a tkinter desktop app for Windows 10 and 11.
- `ctwaits-mcp.exe`: an MCP server over stdio, so Claude Code and Claude Desktop can call the same searches as tools.
- `docs/index.html`: a static web copy of the desktop app for GitHub Pages (see "Web version").

Data comes from Ontario Health's public wait-time APIs (plain GET, JSON, no auth), the same ones behind [www.ontariohealth.ca](https://www.ontariohealth.ca/system/reporting/wait-times). Responses are cached for 24 hours under `%LOCALAPPDATA%\CTWaits\cache`.

The main figure is the 90th percentile wait time in days: 9 in 10 patients waited that long or less. CT, MRI and surgery figures are monthly by priority (P2 urgent, P3 semi-urgent, P4 non-urgent) and go back to January 2022, which is all the history the API holds.

## Layout

```
ctwaits/core.py         data and analysis layer, no UI; both front ends import it
ctwaits/gui.py          tkinter app                      -> dist\CTWaits.exe
ctwaits/mcp_server.py   MCP server (stdio)               -> dist\ctwaits-mcp.exe
ctwaits/contacts.json   researched hospital phone and fax numbers, bundled into both exes
docs/                   static web version: index.html, contacts.js, screening.js
src/build_web_data.py   rebuilds docs/contacts.js and the breast screening snapshot docs/screening.js
.github/workflows/      refresh-web-data.yml (daily breast screening snapshot) and release.yml (builds the exes for a release)
tests/                  pytest suite; tests/fixtures/ holds recorded responses for every endpoint
reference/              the original ct_waits.py script and the handoff brief (local only; .gitignore leaves it out)
build.ps1               venv + dependencies + tests + both PyInstaller builds
requirements.txt        mcp, pytest, pyinstaller (mcp is the only runtime dependency)
```

## Build

Requirements on the build machine: Windows 10 or 11, Python 3.10 or newer on PATH (built and tested with 3.14), PowerShell 5.1 or 7. Target machines need nothing: both exes are self-contained.

```powershell
.\build.ps1
```

The script creates `.venv`, installs `requirements.txt`, runs the test suite, then builds `dist\CTWaits.exe` (`--onefile --windowed`) and `dist\ctwaits-mcp.exe` (`--onefile`, console, so stdio works). Options:

```powershell
.\build.ps1 -Python py          # use a different Python launcher for the venv
.\build.ps1 -VenvDir C:\venvs\ctwaits
.\build.ps1 -SkipTests
```

If PowerShell refuses to run scripts, use `powershell -ExecutionPolicy Bypass -File .\build.ps1`.

Ready-built exes are on the repository's Releases page. Pushing a version tag builds them on GitHub's Windows runners, runs the tests, and attaches both exes to a new release:

```powershell
git tag v1.0.1
git push origin v1.0.1
```

"Run workflow" on the Release workflow (Actions tab) builds the exes as a downloadable artifact without making a release.

Running from source instead:

```powershell
.venv\Scripts\python -m ctwaits.gui          # desktop app
.venv\Scripts\python -m ctwaits.mcp_server   # MCP server on stdio
.venv\Scripts\python -m pytest -q            # tests
```

## Desktop app

**Search form.** Enter a postal code (default XXXXXX) and a radius in km (default 40), then choose Imaging or Surgery:

- Imaging: CT, MRI or breast screening.
- Surgery: type into the procedure box to search the procedure list, then pick one. Choose the wait: decision to surgery (Wait 2, the default) or referral to first appointment (Wait 1).
- Priority: 2, 3 or 4 (default 4). Hover over the (?) beside it for the priority targets: the CT and MRI targets, or for surgery the selected procedure's targets for the chosen wait. Breast screening has no priorities, so the box is disabled.

While a search runs, a progress bar takes the Search button's place; the window stays responsive.

**Search results.** The list is a standard Windows table: headings highlight on hover and sort on click (click again to reverse), and column boundaries can be dragged. For CT, MRI and surgery the columns are Site, km, Latest 90th percentile wait time (days), and 12-mo median. Rows are ranked by the 12-month median, then the latest month. The Ontario provincial average is its own tinted row, placed wherever its figures rank. Sites with no figures in the last 12 months stay at the bottom in grey, whatever the sort. Breast screening shows each site's estimated wait band, nearest first within each band.

**Details.** Click a row to open its details directly underneath it, indented; click it again to close them. The best-ranked site opens automatically after each search. For CT, MRI and surgery the panel shows a graph of every month since January 2022, with the Ontario line and the provincial target for comparison (hover for exact values), and then:

- the address
- contact and fax: the main line, the imaging booking line and the CT or MRI requisition fax, plus any regional intake fax that now takes outpatient requisitions for that site (for surgery: the surgical referral fax where one is published, and the hip and knee central intake fax for hip and knee procedures), with the source pages and the date they were checked
- the share of patients scanned (or treated, or seen) within the target time, on two lines: the latest month, and the last 12 months weighted by each month's patient count
- "Recording data since", the first month the site reported a figure for this priority, with how many months it has reported since

For breast screening the panel shows the address, phone and toll-free numbers, hours, accessibility, languages and the date Ontario Health last updated that site.

**Advanced.** Click "Show advanced" (the down-pointing triangle) to see how well one month's ranking predicts later months. Each lag (1, 3, 6 and 12 months) shows the mean Spearman rho, the share of months where the leader stayed in the top 3, and the chance baseline (3/n).

**Footer and export.** The footer links to www.ontariohealth.ca and shows when Ontario Health last updated the data: the latest reporting month for CT, MRI and surgery, or the latest site update date for breast screening. Export CSV writes the results (and the persistence table) to one file. Errors appear in a message box.

## Web version

`docs/index.html` is the desktop app rebuilt as one static page with the same layout, behaviour, figures and wording. It has no build step and no server code.

- **CT, MRI and surgery** come live from `or.hqontario.ca`, which allows requests from any website.
- **Breast screening** comes from `docs/screening.js`, a province-wide snapshot. The breast screening API only answers pages on www.ontariohealth.ca, so a page on github.io cannot call it. The snapshot is built by querying from every known clinic until no new clinic appears (249 clinics on 2026-10-01).
- **Contacts** come from `docs/contacts.js`, generated from `ctwaits/contacts.json`.

To publish it on GitHub Pages:

1. Push this folder to a GitHub repository (the `.gitignore` leaves out the venv, build output, exes and scratch files).
2. In the repository, open Settings > Pages and choose "Deploy from a branch", branch `main`, folder `/docs`.
3. The site appears at `https://<user>.github.io/<repository>/`.

The workflow in `.github/workflows/refresh-web-data.yml` refreshes the breast screening snapshot every morning and commits it, which redeploys the page. Run it once by hand from the Actions tab to confirm the breast screening API answers GitHub's servers. To refresh by hand instead:

```powershell
.venv\Scripts\python src\build_web_data.py
```

To preview locally, open `docs/index.html` directly, or serve the folder:

```powershell
python -m http.server 8765 --directory docs
```

## MCP server

Five tools, all returning structured data:

| Tool | Arguments | Returns |
| --- | --- | --- |
| `find_procedures` | `query=""` | list of `{id, name}` for surgical procedures |
| `find_sites` | `postal_code`, `radius_km=40`, `modality="CT"`, `procedure_id=None`, `surgery_wait=2` | sites reporting the service, nearest first |
| `rank_sites` | `postal_code`, `modality="CT"`, `priority=4`, `radius_km=40`, `procedure_id=None`, `surgery_wait=2` | `{rows, ontario_average, latest_month, ontario_health_last_updated, persistence, ...}` |
| `site_history` | `site_id`, `modality="CT"`, `months=0`, `procedure_id=None`, `surgery_wait=2` | every month (or the newest `months`) of P90 and % within target by priority |
| `persistence` | `postal_code`, `modality="CT"`, `priority=4`, `radius_km=40`, `procedure_id=None`, `surgery_wait=2` | list of `{lag_months, mean_rho, leader_top3_share, chance_share, pairs}` |

`modality` is CT, MRI or breast screening. Passing `procedure_id` (from `find_procedures`) switches a tool to that surgical procedure. Site id -354 is the Ontario provincial figure. Inputs are validated (postal code format and Ontario-only, priority in {2, 3, 4}, modality, procedure id, surgery wait in {1, 2}); bad inputs come back as a tool error with a plain message.

The server uses the official Python SDK. mcp 2.x renamed `FastMCP` to `MCPServer` (`mcp.server.mcpserver`); the code imports whichever the installed SDK provides, so both 1.x and 2.x work.

### Register with Claude Code

```bash
claude mcp add ct-waits -- "C:\path\to\ctwaits-mcp.exe"
```

Then, in a Claude Code session, ask something like "Rank CT sites within 40 km of POSTAL CODE for a priority 4 scan" or "Where is the shortest knee replacement wait near POSTAL CODE?"

### Register with Claude Desktop

Edit `%APPDATA%\Claude\claude_desktop_config.json` (Settings > Developer > Edit Config) and add the server, then restart Claude Desktop:

```json
{
  "mcpServers": {
    "ct-waits": {
      "command": "C:\\path\\to\\ctwaits-mcp.exe"
    }
  }
}
```

To run from source rather than the exe, point `command` at `C:\\path\\to\\.venv\\Scripts\\python.exe` with `"args": ["-m", "ctwaits.mcp_server"]` and `"cwd"` set to the project folder.

## Data notes

- **Endpoints.** CT and MRI use `/DiagnosticImaging/wtdata/EN/adult/{lat}/{lng}/{page}/{CT|MRI}/1` for the site list and `/DiagnosticImaging/wtchartdata/EN/adult/{CT|MRI}/1/{site_id}` for history. Surgery uses `/Surgical/wtsurgicaldata/...` and `/Surgical/wtsurgicalchartdata/...` with the procedure's type and modality ids and the wait (1 or 2). The procedure list comes from `/surgery/getsurgeriesbyname/surgical/EN/adult/`. Breast screening uses `https://obspwaittimeapi.ontariohealth.ca/api/obsp`.
- **The original "MRI trap".** The site list endpoint takes the modality as its second-to-last path segment. The first version of this tool put `1` there, which the API treats as MRI. The list is now requested per modality, so MRI-only clinics no longer appear in CT results. Wait numbers still come only from the per-site history endpoints, and a test guards this.
- **Missing values.** `RI` (reported insufficient), `LV` (too few cases to report), `NV` (no cases in the period), `NS` (not performed here), blanks and missing values all become null.
- **Provincial row.** The first row of every list is a provincial total (Id -354). It is dropped from the site list; the provincial history is fetched separately and shown as the Ontario provincial average.
- **Adult only.** The tool queries the adult series. The Hospital for Sick Children (Id 4824) appears in adult lists with no adult figures, so it is excluded.
- **Short series.** Some sites (University Health Network) have only a few months of history. They are ranked on what they have, and "Recording data since" shows when they started reporting.
- **Breast screening search.** The API returns only the 50 sites nearest the point it is given and ignores paging. The app therefore queries extra points: when one answer does not reach the edge of its area, the area is split into seven smaller ones and each is queried (about a dozen queries for downtown Toronto at 40 km, which finds 97 sites). Distances are straight-line kilometres from the postal code. Breast screening has a current estimate only, so there is no history, priority or provincial figure.
- **Contact details.** Neither API publishes hospital phone or fax numbers, and Ontario's provincial service-location open data has addresses only. `ctwaits/contacts.json` holds numbers researched on 2026-10-01 from hospital websites and requisition forms for the 193 sites that report CT, MRI or surgery; 181 have at least one number. Each number was checked against the text of its cited page: 459 were found, 27 sit on pages that could not be re-read (the app says so), and 6 that were missing from their pages were dropped. Confirm a fax number before sending patient information. Breast screening phone numbers come from the API.
- **Regional intake faxes.** Several regions now route outpatient CT and MRI requisitions through one fax. Ontario Health West's central intake (365-317-9260) covers every hospital in the region; from November 2, 2026 it returns requisitions faxed straight to a hospital, though stat requests and OCEAN e-referrals still go direct. The Central Region hub, the North East hub, the Northwest CT intake and the Eastern Ontario MRI intake are shown for the sites whose own pages point to them. Hip and knee central intake faxes are shown only for hip and knee procedures, never as a general surgical fax.
- **Persistence lags** count steps along the observed series of months, matching the reference script.
- **Refreshing.** To force a refresh before the 24-hour cache expires, delete `%LOCALAPPDATA%\CTWaits\cache`.

## Tests

```powershell
.venv\Scripts\python -m pytest -q
```

The suite uses recorded JSON fixtures in `tests/fixtures/` (captured 2026-10-01) and never touches the network. It covers null-code parsing, the provincial row, pagination stop conditions, per-service URLs, the procedure search, breast screening parsing and paging, the Spearman calculation against hand-computed cases, ranking, persistence, CSV export, and a guard that wait numbers never come from the list endpoints.
