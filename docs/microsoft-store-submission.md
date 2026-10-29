# Building and submitting Elanero to the Microsoft Store

A step-by-step guide for the account owner. It takes you from a clean checkout to a published
Store listing. Part 1 builds and tests the package on your PC, and Part 2 submits it in
Partner Center. Part 3 covers later updates.

Partner Center's menus change from time to time. If a label here doesn't match exactly, look
for the closest one; the steps stay the same.

---

## Part 1: Build the package

### 1.1 One-time setup on your PC

1. **Windows SDK.** Check that `makeappx.exe`, `makepri.exe` and `signtool.exe` exist under
   `C:\Program Files (x86)\Windows Kits\10\bin\<version>\x64\`, and that the App
   Certification Kit is in `C:\Program Files (x86)\Windows Kits\10\App Certification Kit\`.
   If not, install the Windows 11 SDK from
   <https://developer.microsoft.com/windows/downloads/windows-sdk/> (the defaults include all
   four tools).
2. **Developer Mode** (for the local install test in 1.5): Windows Settings ▸ System ▸ For
   developers ▸ **Developer Mode: On**.
3. **Python environment**, from the repository folder:

   ```powershell
   uv venv --python 3.11 .venv
   uv pip install -e ".[dev,build]"
   ```

4. **Local PyInstaller launcher.** Install Visual Studio Build Tools with the "Desktop
   development with C++" workload, then run, once per environment:

   ```powershell
   uv run python scripts/build_bootloader.py
   ```

   It compiles PyInstaller's launcher on your PC. The prebuilt one matches malware packed with
   PyInstaller, and the Store's security scan rejected 0.1.0 because of it (issue #90). The
   MSIX build stops with a message if you skip this. Run it again after reinstalling or
   upgrading PyInstaller.
5. **Store identity.** `packaging\msix\identity.json` must contain the values from Partner
   Center ▸ Apps and games ▸ Elanero - PDF Editor ▸ Product management ▸ **Product
   identity**, with `"reserved": true`. You have already done this. The file is git-ignored:
   keep a copy somewhere safe, because a fresh clone won't have it.

### 1.2 Start from a clean, tagged commit

The AGPL requires that every build you distribute matches public source code.

```powershell
git checkout main
git pull
git status                      # must show no modified files
```

Set the version in `src\pdfeditor\__init__.py` (`__version__`) and in `pyproject.toml`
(`version`). It is currently `0.1.1` (0.1.0 was the first, rejected upload). The Store
needs a final `X.Y.Z` version (no `rc`/`b`/`dev`), and every upload must be higher than
the last.

Then tag the commit and push the tag:

```powershell
git tag -a v0.1.1 -m "Elanero 0.1.1"
git push origin v0.1.1
```

### 1.3 Build

```powershell
uv run python scripts/build_exe.py --msix --test --clean-env
```

This takes 2–4 minutes and does the following:

1. builds the app as a folder (`dist\Elanero\`) and runs its self-test, which must print
   `RESULT: OK`;
2. runs it again from a copy in an empty profile, with no Python on PATH, and opens a sample
   PDF (`clean environment: window with sample.pdf after …s`);
3. creates the package `dist\Elanero-0.1.1-x64.msix` and checks that every file made it in.
   Lines like `makeappx: … file(s) left out … retrying` are normal; the build fails rather
   than produce an incomplete package.

**Don't add `--sign-test` for a Store upload.** The Store signs the package itself; the test
signature is only for installing on other test machines.

Check the end of the output:

- `built …\dist\Elanero-0.1.1-x64.msix (about 70 MB)`, and
- **no** line saying `note: building with the test identity`. If you see it,
  `identity.json` is missing, so the package has the test identity and the Store will reject
  it.

To double-check the identity inside the package:

```powershell
uv run python -c "import zipfile,re; m=zipfile.ZipFile('dist/Elanero-0.1.1-x64.msix').read('AppxManifest.xml').decode(); print(re.search(r'<Identity[^>]*>', m).group(0))"
```

It must show `Name="SGNSolutions.Elanero-PDFEditor"`,
`Publisher="CN=…"` (your publisher ID from Product identity) and `Version="0.1.1.0"`.

### 1.4 Run the Windows App Certification Kit

This runs the same technical checks as Store certification. It needs administrator rights.

**Option A, the app:** Start ▸ type **Windows App Cert Kit** ▸ right-click ▸ **Run as
administrator** ▸ **Validate Store App** ▸ browse to `dist\Elanero-0.1.1-x64.msix` ▸ select
all tests ▸ Next. It takes 5–15 minutes; leave the PC alone while it opens and closes the
app. Save the report when asked.

**Option B, the command line**, in an administrator PowerShell in the repository folder.
Paste it as one line (the `;` separates the commands):

```powershell
$kit = "C:\Program Files (x86)\Windows Kits\10\App Certification Kit\appcert.exe"; & $kit reset; & $kit test -appxpackagepath "$PWD\dist\Elanero-0.1.1-x64.msix" -reportoutputpath "$PWD\build\msix\wack.xml"
```

The overall result must be **PASSED** or **WARNING**. Two results are expected and don't block
submission:

- the optional **Blocked executables** test fails on text such as `cmd` and `reg` inside the
  Python, Qt and MuPDF libraries;
- **High-DPI support** warns because Qt sets PerMonitorV2 when it starts, not in the exe's
  manifest.

If any other test fails, send me the report (`wack.xml` or the saved HTML) before uploading.

### 1.5 Install it on your PC once

With Developer Mode on, install the package exactly as built, try it, then remove it:

```powershell
$mk = Get-ChildItem "C:\Program Files (x86)\Windows Kits\10\bin\*\x64\makeappx.exe" | Select-Object -Last 1
& $mk.FullName unpack /p dist\Elanero-0.1.1-x64.msix /d build\msix\installed /o
Add-AppxPackage -Register build\msix\installed\AppxManifest.xml
```

Then:

- Start ▸ **Elanero - PDF Editor** opens the app, with the right icon in Start and on the
  taskbar.
- In a new terminal, `elanero --self-test --self-test-report $env:TEMP\st.txt` and then
  `Get-Content $env:TEMP\st.txt` must show `channel: Microsoft Store` and `RESULT: OK`.
- Right-click a PDF in Explorer ▸ **Open with**: Elanero is listed and opens it.
- Help ▸ About shows `(Microsoft Store)`.

Remove it again (your own settings from the portable version aren't affected):

```powershell
Get-AppxPackage *Elanero-PDFEditor* | Remove-AppxPackage
```

### 1.6 Prepare the images

Partner Center needs these for the Store listing:

| Image | Required? | Size | Notes |
|---|---|---|---|
| Desktop screenshots | **Yes, at least 1** (up to 10) | 1366×768 or larger, PNG | 4 or more recommended, e.g. the start page, editing text, comments, the page organizer, light and dark |
| 1:1 Store logo (box art) | Recommended | 1080×1080 or 2160×2160, PNG | Shown on the Windows 10/11 Store page; without it the package logo is used |
| 2:3 poster art | Recommended | 720×1080 or 1440×2160, PNG | Shown in some Store layouts |

To take screenshots yourself: open a realistic sample document, maximize the window on a
1920×1080 screen (or set the display to that resolution), and press **Win+Shift+S ▸ Window
mode**. Avoid personal file names in the title bar or recent files list. I can also generate
these (PLAN.md S6).

---

## Part 2: Submit in Partner Center

Go to <https://partner.microsoft.com/dashboard/apps-and-games/overview> ▸ **Elanero - PDF
Editor** ▸ **Start your submission**. The submission page lists sections; each must show
**Complete** before you can submit. They can be filled in any order and are saved as you go.

### 2.1 Pricing and availability

1. **Markets:** keep **all markets** (or remove countries you don't want).
2. **Visibility:** for the first release choose **Private audience**, so only you and people
   you invite can find and install it:
   - Click **Create a new group** (or go to Customers ▸ Customer groups ▸ Create).
   - Name it `Elanero testers` and add the email addresses of Microsoft accounts that may
     install it, starting with your own.
   - Select that group.

   After certification you'll switch this to **Public audience** (2.8).
3. **Schedule:** **Release as soon as possible**. The publishing hold in 2.6 decides when it
   goes live.
4. **Pricing:** Base price **Free**. No free trial and no sale pricing.
5. **Save.**

### 2.2 Properties

1. **Category:** **Productivity**; no subcategory.
2. **Privacy policy URL:** `https://github.com/linguagraph/pdf-editor/blob/main/PRIVACY.md`
3. **Website:** `https://github.com/linguagraph/pdf-editor`
4. **Support contact info:** `https://github.com/linguagraph/pdf-editor/issues` (or a
   support email address you want to publish).
5. **Product declarations:**
   - Leave **"This app has been tested to meet accessibility guidelines"** unticked for now
     (PLAN.md U11 hands-on is still open).
   - Tick **"Customers can install this app to alternate drives or removable storage"** (it
     works from any drive).
   - Leave the rest at their defaults.
6. **System requirements:** Minimum hardware: **x64**; memory 4 GB is a safe statement; leave
   DirectX and other fields blank.
7. **Save.**

### 2.3 Age ratings

1. Click **Age ratings** and start the **IARC questionnaire**.
2. Choose the category that fits a PDF editor, typically **Utility, productivity,
   communication or other**. Answer the questions truthfully:
   - The app doesn't let users interact with or share content with other users online.
   - There's no in-app purchase, gambling, violence, sex or drug content.
   - It doesn't share the user's location.
   - It **can** access the internet: only to download OCR language files when the user asks.
3. Submit the questionnaire. The expected result is **3+ / Everyone** in all regions.
4. **Save.**

### 2.4 Packages

1. Drag `dist\Elanero-0.1.1-x64.msix` onto the upload box. Upload the unsigned package from
   1.3, not one built with `--sign-test`.
2. Wait for validation (a minute or two). The package should appear as
   `SGNSolutions.Elanero-PDFEditor 0.1.1.0, x64`, with no errors.
   - **"Invalid package identity name" / "publisher"**: the package was built without your
     `identity.json`. Rebuild (1.3) and check the identity line.
   - **"Restricted capability runFullTrust"**: expected. Partner Center asks you to justify
     it in Submission options (2.6).
3. **Device family availability:** tick **Windows 10/11 Desktop** only. Untick Xbox,
   HoloLens, Team and others.
4. Leave **"Let Microsoft decide whether to make this app available to future device
   families"** unticked.
5. **Save.**

### 2.5 Store listings

1. Under **Store listings**, open **English (United States)**. Add other languages later if
   you translate the listing.
2. Copy each field from `packaging\store\listing-en.md`:

   | Partner Center field | Section in `listing-en.md` |
   |---|---|
   | Product description | "Description" |
   | What's new in this version | "What's new in this version" |
   | Product features | "Product features" (one per line, up to 20) |
   | Short description | "Short description" |
   | Search terms | "Search terms" (up to 7) |
   | Copyright and trademark info | "Support and legal" ▸ Copyright |
   | Additional system requirements | "System requirements" |
   | Additional license terms | `GNU AGPL-3.0-or-later: https://github.com/linguagraph/pdf-editor/blob/main/LICENSE` |
   | Developed by | your display name (`SGN Solutions`) |

3. **Screenshots ▸ Desktop:** upload the screenshots from 1.6 and add a one-line caption to
   each, e.g. "Edit text directly in your PDF".
4. **Store logos:** upload the 1:1 and 2:3 images if you have them.
5. Leave **Trailers** empty.
6. **Save.**

### 2.6 Submission options

1. **Publishing hold options:** choose **"Don't publish this submission until I select
   Publish now"**. You then decide when it goes live after certification.
2. **Notes for certification:** paste the "Notes for certification" section from
   `listing-en.md`. Testers need no account or password.
3. **Restricted capabilities ▸ Why do you need the runFullTrust capability?** Paste:

   > Elanero is a full-trust desktop PDF editor. It needs runFullTrust because it opens and
   > saves files anywhere the user chooses, prints, starts additional windows of itself, and
   > optionally starts LibreOffice, if installed, to convert Office files.

4. **Save.**

### 2.7 Submit

1. Back on the submission overview, every section should say **Complete**.
2. Click **Submit to the Store**.
3. The status page shows **Pre-processing ▸ Certification ▸ Release**. Certification usually
   takes a few hours and up to 3 business days. You'll get an email when it finishes.
4. **If certification fails**, the status page links a **certification report** listing each
   problem and the policy section. Send it to me; after the fix, rebuild, upload the new
   package in the same submission and resubmit. If Partner Center refuses the same version
   number, raise the version (Part 3, step 1).

### 2.8 Publish

1. When certification passes, the status shows **"Ready to publish"** or similar (because of
   the publishing hold). Click **Publish now**. Within about an hour the app can be
   installed by the people in your private audience.
2. Find the Store link: Product management ▸ Product identity ▸ **URL** (looks like
   `https://apps.microsoft.com/detail/9N…`). Note the **Store ID** (`9N…`) too; the GitHub
   Action needs it.
3. **Test the real Store install** on a clean PC or a fresh Windows user signed in with an
   account from your testers group: open the link ▸ **Get** ▸ open Elanero ▸ open a PDF from
   Explorer ▸ Help ▸ About shows `(Microsoft Store)` ▸ uninstall from Settings ▸ Apps.
4. **Go public:** start a new submission (Partner Center copies the last one), open
   **Pricing and availability**, set **Visibility** to **Public audience**, then submit. This
   resubmission is usually certified quickly, since nothing else changed.

### 2.9 After publishing

- Add the Store link to the README ("Get it from Microsoft" badge) and to `RELEASING.md`; I
  can do this once you send me the URL.
- Create the GitHub release for `v0.1.1` and attach `Elanero.exe`, the installer and the
  `.msix`.
- Tick the matching boxes in the checklist on issue #88.

---

## Part 3: Releasing an update

1. Raise the version (e.g. `0.1.1`) in `src\pdfeditor\__init__.py` and `pyproject.toml`,
   merge, then tag `v0.1.1` and push the tag.
2. Build and test exactly as in 1.3–1.5.
3. In Partner Center ▸ Elanero ▸ **Start update** (it copies the previous submission):
   - **Packages:** remove the old package and upload the new `.msix`.
   - **Store listings:** update **What's new in this version**.
   - Leave everything else unless it changed, then **Submit to the Store**.
4. Users get the update automatically from the Store, usually within a day of publishing.

Pushing the tag also runs the Release workflow (RELEASING.md). After you approve the
`microsoft-store` deployment in GitHub ▸ Actions, it builds the package and uploads it as a
draft submission, so step 2 and the package upload in step 3 are done for you. Open the draft
in Partner Center, update **What's new**, and submit.

---

## Troubleshooting

| Problem | Cause and fix |
|---|---|
| Build prints `note: building with the test identity` | `packaging\msix\identity.json` is missing or unreadable. Restore it from your copy. |
| `makeappx … failed` or `couldn't pack a complete MSIX` | Usually antivirus scanning the new files. Wait a minute and run the build again. |
| `makepri.exe not found` / `makeappx.exe not found` | Install the Windows SDK (1.1). |
| `Add-AppxPackage … 0x80073CFF` during the local test | Developer Mode is off (1.1). |
| `Add-AppxPackage … 0x80073CF6` mentioning resources | Rebuild; send me the full message if it persists. |
| MSIX build stops: "the prebuilt launcher from PyInstaller's wheel is in use" | Run `uv run python scripts/build_bootloader.py` (1.1, step 4). |
| Partner Center: "Malware was detected in your package", naming `Elanero.exe` | A false positive. Report the package at <https://www.microsoft.com/wdsi/filesubmission> (Software developer ▸ Incorrectly detected) and resubmit once it's cleared. Check that the build used the local launcher (`build_bootloader.py --check`). |
| Partner Center: "The package version must be higher" | Raise `__version__` (Part 3, step 1). |
| Partner Center: identity or publisher mismatch | The package was built with different identity values from Product identity. Compare them (1.3). |
