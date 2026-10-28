# Releasing Elanero

Every release ships three ways from the same source tag:

- **Microsoft Store:** `Elanero-<version>-x64.msix`. Microsoft signs it and updates users.
- **Portable:** `Elanero.exe`.
- **Installer:** `pdfeditor-<version>-setup.exe`, made with Inno Setup.

The portable exe and the installer are unsigned until we have a code-signing certificate
(PLAN.md Phase 17 and Phase 18 S10).

## One-time setup

- **Windows SDK:** provides `makeappx`, `makepri`, `signtool` and the App Certification Kit
  (`appcert`), used only at build time.
- **Store identity:** copy `packaging/msix/identity.example.json` to
  `packaging/msix/identity.json` and fill in the values from Partner Center ▸ Elanero ▸
  Product management ▸ Product identity:

  | Partner Center | `identity.json` |
  |---|---|
  | Package/Identity/Name | `name` |
  | Package/Identity/Publisher (`CN=…`) | `publisher` |
  | Package/Properties/PublisherDisplayName | `publisher_display_name` |

  Then set `"reserved": true`. `identity.json` is git-ignored and must not be pushed. Without
  it the build uses the example's test identity, which the Store rejects.

## Making a release

1. **Version.** Set `__version__` in `src/pdfeditor/__init__.py` (and `pyproject.toml`). The
   Store version is `X.Y.Z.0`, and each upload must be higher than the last.
2. **Checks.** `ruff check`, `ruff format --check`, `lint-imports`, `mypy` and `pytest` all
   pass, and CI is green on `main`.
3. **Tag.** Tag the release commit and push the tag, so every shipped build has public source
   (AGPL):

   ```sh
   git tag -a vX.Y.Z -m "Elanero X.Y.Z" && git push origin vX.Y.Z
   ```

4. **Build** from the tagged commit:

   ```sh
   uv run python scripts/build_exe.py --test --clean-env           # dist/Elanero.exe
   uv run python scripts/build_exe.py --msix --test --clean-env    # dist/Elanero-X.Y.Z-x64.msix
   ```

   The MSIX step checks that every file made it into the package (makeappx sometimes skips
   freshly written files, so it retries), and fails rather than produce a short package.
5. **Test the package locally.** With Developer Mode on, unpack and register it, run the
   self-test through its alias, then remove it:

   ```powershell
   makeappx unpack /p dist\Elanero-X.Y.Z-x64.msix /d build\msix\installed /o
   Add-AppxPackage -Register build\msix\installed\AppxManifest.xml
   elanero --self-test --self-test-report $env:TEMP\st.txt; Get-Content $env:TEMP\st.txt
   Get-AppxPackage *Elanero-PDFEditor* | Remove-AppxPackage
   ```

   The report must say `channel: Microsoft Store` and end with `RESULT: OK`.
6. **Windows App Certification Kit.** In an **administrator** PowerShell (the kit needs
   elevation):

   ```powershell
   & "C:\Program Files (x86)\Windows Kits\10\App Certification Kit\appcert.exe" reset
   & "C:\Program Files (x86)\Windows Kits\10\App Certification Kit\appcert.exe" test `
       -appxpackagepath dist\Elanero-X.Y.Z-x64.msix -reportoutputpath build\msix\wack.xml
   ```

   Fix any failed test before uploading. Warnings about the Python runtime's DLLs are expected
   to be informational; check each one.
7. **Upload.** In Partner Center ▸ Elanero, start a submission:
   - **Packages:** upload the unsigned `.msix` from step 4 (build without `--sign-test`).
   - **Store listings:** paste from `packaging/store/listing-en.md`, and add the screenshots
     and images.
   - **Pricing:** free.
   - **Properties:** category Productivity; privacy URL from the listing file.
   - **Age ratings:** the IARC questionnaire; there's no user-generated content shared online.
   - **Submission options:** paste the certification notes from the listing file.

   The first submission goes to a private audience first; make it public once certified.
8. **GitHub release.** Attach `Elanero.exe`, the installer and the `.msix` to the release for
   the tag.

## Licensing (AGPL)

- Every build we distribute must correspond to a public tag; don't upload a build made from
  uncommitted changes.
- The package contains `LICENSE` and the third-party notices (Help ▸ About shows both, with
  the source link).
- The Store description links to the source.
