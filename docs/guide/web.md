# Web version

Elanero also runs in your web browser, with no installer. It is the same editor as the desktop
app, rebuilt for the browser: the same commands, names and icons, so what you learn here carries
over to the desktop. Most of the web version is still being built. This page describes what works
today and marks what is coming soon.

## How it differs from the desktop app

- **It runs in the browser tab.** Nothing to install on Windows, Mac or iPad: open the web address
  and the editor loads.
- **It works offline after the first load.** The browser keeps the app's own files and the PDF
  engine's files, so the editor opens without a connection. Your documents are not uploaded: they
  stay in the browser, which keeps a private autosave copy of unsaved changes (see
  [Where your data is stored](#where-your-data-is-stored)).
- **It can be installed as an app.** The browser can add Elanero to your desktop, dock or home
  screen and open it in its own window.
- **The browser replaces some desktop features.** There is no menu bar and no separate window
  or Exit command: the browser provides them. The app menu (☰) holds the file, settings and help
  commands; its **Install App** command is coming soon.
- **Some features are desktop only.** OCR, digital signatures and PDF/A conversion are not planned
  for the web version yet. See the table at the end of this page.

## Start the web version

Open the web version from the Elanero website: the **Open the web version** button on the front
page. The web address is <https://linguagraph.github.io/elanero-pdf-editor/app/>, which becomes
live when the web version is published. Until then the button does not work yet; use the desktop app.

The first visit loads the editor's engine, which takes a little while. Later visits are faster
because the browser keeps the app's files.

A password-protected PDF asks for its password when it opens. The password is used only to open that
file: it is not stored, and it is not sent anywhere.

You don't have to wait for the engine. You can open or drop a PDF while Elanero starts: it is listed
under "Waiting to open" and opens as soon as the engine is ready. To drop a waiting file, press
**Don't open** beside its name.

## Install the app

Use your browser's own install option. In Chrome and Edge, use the install icon in the address bar
or the browser menu. In Safari, use the browser's **Add to Dock** (Mac) or **Add to Home Screen**
(iPad) option. Elanero's **Install App** command in the app menu is coming soon.

Once installed in Chrome or Edge, Elanero is an **Open with** choice for PDF files. Right-click a PDF
in Explorer (or Finder), choose **Open with**, then **Elanero**: the file opens in a tab. Selecting
several PDFs opens them together, as tabs in one window. If Elanero is already open, the files open
in that window and your open documents stay as they are. Up to 20 files open from one choice; the
others are left for the app's Open button. Other browsers and the plain browser tab have no
**Open with**: use Open or drag and drop.

When a new version of Elanero is ready, a banner appears at the top with a **Reload** button. Nothing
is reloaded until you press it.

## Theme and preferences

- **Theme.** The switch in the top bar chooses **System** (the default: follows your computer's
  light or dark setting, and changes with it), **Light** or **Dark**. The choice is remembered in
  this browser.
- **Preferences.** Click **Preferences** in the top bar. The dialog has three sections:
  - **General**: the language (English is the only one for now) and the **Author name**, which is
    saved with each comment and redaction mark you add.
  - **Appearance**: the theme, the **Accent colour** (Blue, Teal, Green, Purple, Pink, Red, Orange
    or Graphite: it colours buttons and highlights; Blue is the default), and **Reduce motion**,
    which turns off animations. The accent keeps the system's high-contrast colours when Windows
    high contrast is on.
  - **Viewing**: **Default zoom** (Fit Width, Fit Page or 100%) and **Default page layout** (Single
    column, Single page, Two pages, or Two pages with page 1 alone), both used when a document is
    opened.
  - **Redaction**: the **Box colour**, **Text colour** and **Text on the box** used for new redaction
    marks (black, white and empty by default). Marks you have already drawn keep their style.
  - **Shortcuts**: **Keyboard Shortcuts…** lists every keyboard shortcut. It opens on top of
    Preferences, and your unsaved changes there are kept.
  - **Privacy**: **Clear all local data…** removes your saved preferences, recent file names and
    uploaded fonts from this browser, then reloads the page. It is turned off while a document is
    open; close the documents first. Your files are not touched, and the recovery copies of other
    open Elanero tabs are kept.

  **Apply** saves your changes; **Cancel**, Esc or a click outside the dialog puts back the saved
  theme. The default zoom applies to the open document at once, and to each document you open. The
  author name is written into each comment and redaction mark you add from now on.

  Zoom above 100% on a high-density screen can ask for more pixels than a page can be drawn at. The
  viewer then draws the page at the largest size it supports, so the page stays visible rather than
  going blank.

## Ribbon and keyboard shortcuts

- **Ribbon.** The ribbon sits under the top bar with the same seven tabs as the desktop (Home, Edit,
  Organize, Protect, Comment, View and Tools). Each command shows its icon above its label, the same
  icon as on the desktop. A command that is not available in the web version yet, or that needs an
  open document, stays visible but greyed out; point at it to see why.
- **Ribbon forms.** The ribbon follows the window width. From 1280 px it shows the full ribbon, with
  icons and labels. From 1024 px to 1279 px it is compact: icons only, with each command's name in
  its tooltip. Below 1024 px the ribbon is collapsed to one row of tabs; each tab opens its commands
  in a drop-down that closes when a command runs. On a narrow window that row scrolls sideways.
- **More commands.** When a tab's groups are wider than the window, the last groups move to a
  **»** (More commands) button at the end of the row. It opens those groups, in the same order,
  and closes when a command runs. A tab with room for all its groups shows no **»**.
- **Collapse and compact.** The chevron at the right of the tab row collapses the ribbon to its
  tabs, and shows it again. Press **Ctrl+F1** to do the same. Pressing a tab, or double-clicking
  one, also brings a collapsed ribbon back. **Compact ribbon** in Preferences (Appearance) shows
  icons only on wide windows as well (1280 px and up). Both choices are kept in this browser.
- **Quick access and help.** Above the ribbon, next to the menu button, the quick-access buttons
  are Select, Hand, Undo, Redo and Save; below 1024 px only Undo, Redo and Save remain. The **?**
  button at the right of the tab row opens this guide at the page for the tab you are on. **About Elanero** in the app menu shows the version, the licence and
  a link to the source code. Close in the app menu closes the open document. Select and Hand switch
  the tool of the open tab (see Hand tool below). Save works (see Saving below).
- **Undo and Redo.** Undo (Ctrl+Z) and Redo (Ctrl+Y) step back and forward through the changes to
  the open document, in the Home tab's History group and in the quick-access buttons. Each is
  enabled only when there is a step to take; with no document open, the disabled note says so.
  Each document keeps its own steps, and they are cleared when the document is closed.
- **Search tools.** The box at the right of the tab row (the search icon alone in a narrow window
  or a compact ribbon) opens the command palette. Type in the box and the palette opens already
  filtered by what you typed; the icon opens the palette empty.
- **View and navigation.** The View tab's zoom commands (Zoom In and Out, Fit Width, Fit Page and
  Actual Size) and its page layouts (Single Page Continuous, Two-Up Continuous and Two-Up with
  Cover Page) work on the open document. The layout you are in shows as pressed, and so does Night
  Mode. Home's Back and Forward follow the view's history, and Previous and Next Page are greyed out
  on the first and last page. Organize Pages opens the organizer panel. The document's layout
  buttons also offer **Single page**, which shows one page at a time: Previous and Next Page, the
  page field and Home and End move between pages, and the page you were on stays the page when you
  change layout. The view rotation is not available yet.
- **Hand tool.** Choose **Hand** (or press **H**) and a drag with the mouse or a pen moves the pages
  under the pointer. One-finger touch still scrolls the page as the browser does. Choose **Select**
  (or press **V**) to go back. Hand changes only the view: it does not change the document, and it is
  not kept when you reload the page.
- **Copy and Select All.** Drag over the text of a page to select it, then choose **Copy** in the
  Home tab's Tools group or press **Ctrl+C**: the selected text goes to the clipboard, with a line
  break where each line ends and between pages. **Select All** (Ctrl+A, also in the Home tab) selects
  the text of the page at the top of the view. Copy says so when nothing is selected, and when the
  selection is too large to copy at once (about 50 MB of text). A scanned page has no text to copy.
  Copy can also say that the text of a selected page is still loading: try again in a moment. With
  **Hand** chosen, Copy and Select All use the browser's own selection instead.
- **Keyboard shortcuts.** A few web keys work so far: **Ctrl+C** (Copy) and **Ctrl+A** (Select All), **Alt+Left** and **Alt+Right** go back and
  forward through the pages you have visited, **Alt+G** (Go to Page) moves the cursor to the page
  box in the status bar, and **Alt+PgDn** and **Alt+PgUp** switch to the next and previous open
  document, wrapping at the ends. Go to Page and the two document keys are also in the command
  palette. The web version will use the desktop's keys wherever the browser allows, and will list
  them in [Keyboard shortcuts](keyboard-shortcuts.md) once every shortcut works in the web version (coming
  soon).
  **Keyboard Shortcuts…** in the app menu and in Preferences shows the list that is available now.

## Side panels

- **Thumbnails.** The **Thumbnails** button on the right rail opens a panel with a small preview of
  every page. Click a preview to go to that page. The panel opens and closes for each open
  document separately, and it is not remembered after a reload. Layers are not available yet.
- **Bookmarks.** The **Bookmarks** button on the right rail opens the document's outline as a tree.
  Use the arrow beside an entry to expand or collapse it, and click an entry to go to its page.
  With the keyboard, Up and Down move through the entries, Right and Left open and close them,
  and Enter goes to the page.
  Entries without a page are greyed out. Where the document can be edited, a toolbar sits above the
  list, and the entry you are on is highlighted. Press F2 to rename it (type the new title, then
  Enter keeps it and Escape cancels). Press Delete to remove it with everything nested under it.
  Press Alt+Shift+Up or Alt+Shift+Down to move it among its neighbours, and Alt+Shift+Right or
  Alt+Shift+Left to indent it under the entry above or outdent it. The toolbar's Set to Current
  Page points the entry at the top of the page now on screen. Each change can be undone from the
  message that follows it. Adding bookmarks is coming soon. Each open document keeps its own
  expanded entries until you close it.
- **Attachments.** The **Attachments** button on the left rail lists the files embedded in the
  document, with the size of each. Click a file to see its name, size and description, then press
  **Save** to download it to your computer. The file is saved as it is; the browser never opens or
  runs it. If the document has no attachments, the panel says so. The panel is read-only for now:
  you cannot add or remove attachments yet.
- **Layers.** The **Layers** button on the left rail lists the optional content layers of the
  document, in the order the file gives them. Each row shows whether the layer is visible in the
  document's default view, and a layer that the file locks is marked **Locked**. Nested layers
  are indented. If the document has no layers, the panel says so. The panel is read-only for now:
  you cannot show or hide a layer yet.
- **Search.** The **Search** button on the left rail opens a panel that finds text in the open
  document. Type in the box and press Enter (or **Search**). The search starts at the page you are
  on and wraps round to the first page. Each match is listed with its page and the text around it,
  and the current match is outlined on the page. **Previous match** and **Next match** move between
  the matches; while the box still holds the query searched, Enter and Shift+Enter do the same, and
  F3 and Shift+F3 do it from the keyboard. **Match case**, **Whole words** and **Regular expression**
  change how the text is matched; a pattern that does not work is refused, and the panel says why.
  A long search runs in parts: **Stop** ends it and keeps the matches found so far, and a new search
  replaces it. Pages with no text, such as scanned pages, have nothing to find. Searching only reads
  the document, so it is not part of Undo.
- **Comments.** The **Comments** button on the left rail lists the document's comments, one entry
  per thread, with the replies under their comment. Each entry shows its page, type, author, date,
  status and text. Search the text and authors, or choose a type, author or status to narrow the
  list; a thread stays whole when only one of its replies matches. Click an entry (or press Enter
  on it) to go to its page. The list is in page order, top to bottom. Each thread takes a reply and a new status, and Undo
  reverses either; editing and deleting a comment from the panel are coming soon. The panel opens
  and closes for each open document, and the list is read again after each change, undo, redo and
  save. A very long list is cut short, and the panel says so.
- **Organize pages.** The **Organize pages** button on the right rail opens a panel with a
  thumbnail of every page. Click a page to select it, Ctrl-click to add or remove one, and
  Shift-click to select a range. Then rotate the selected pages left or right, move them up or
  down, delete them (after a confirmation; a document always keeps one page), or duplicate them.
  Insert blank page adds an empty page after the last selected page (or at the end when nothing is
  selected), the same size as the page before it. Duplicate puts a copy of each selected page right
  after the last one. Insert from file adds the pages of a PDF, or one image, after the last selected
  page (or at the end when nothing is selected); an encrypted PDF asks for its
  password, up to five times. Extract downloads the selected pages as a new PDF named
  "<name> (extract).pdf"; the document itself does not change. Each change, other than an
  extract, can be undone from the notice that follows it. Undo on a notice undoes only that change;
  if you have changed the document since, nothing is undone and the notice says so.
  To move pages by dragging, press a page and drag it with the mouse or a pen (a selection moves
  as a block). A line shows where the pages will land, and Escape cancels the drag. A drop that
  changes nothing does nothing. Touch has no drag: use Move up and Move down instead.
- **Redactions.** The **Redactions** button on the left rail lists every redaction mark in the
  document: the marks you drew, the ones an undo brought back, and the ones already in the file.
  Each row shows its page, the text it covers (or "Area") and its overlay text, with a swatch of its
  box colour. Click a row to select it; Ctrl-click or Space adds one, and Shift-click selects a range.
  Enter or a double-click goes to the mark's page, and the selected boxes are outlined on the page.
  Escape clears the selection. With a selection you can change its **Fill**, **Text colour** and
  **Overlay text** (up to 500 characters, saved when you press Enter or leave the field). When the
  selected marks differ in a field, that field shows "Mixed", and a change sets only that field on
  every selected mark. A locked mark cannot be changed. **Delete** removes the selected marks.
  **Apply Selected** and **Apply All** burn the marks into the document after you confirm; Apply All
  includes the marks already in the file. Marks are added with **Mark areas** on the document toolbar.
- **Mark Text.** On the **Protect** tab, **Mark Text** marks the text you selected with the Select tool
  (a drag, or Select All) as redaction marks: one mark per page the selection covers, all in one undo
  step. It only marks. Nothing is
  removed until you apply the marks, from the Redactions panel or the toolbar's **Apply**. With no text
  selected, the command asks you to select some first. A mark keeps the selected text in its note until
  the mark is applied, and applying the marks removes that text from the file.
- **Mark Pages.** On the **Protect** tab, **Mark Pages** opens a dialog to choose the pages to mark
  whole: the current page (the default), all pages, or a range such as `1-3, 5`. Each chosen page gets
  one mark that covers the whole page, all in one undo step. When more than one page is chosen, the
  dialog says how many it will mark. It only marks: nothing is removed until you apply the marks, so
  the page stays readable until then. A bad range is refused inside the dialog. The Redactions panel
  does not open; a message says how many pages were marked.
- **Comment tools.** On the **Comment** tab, **Note**, **Highlight** and **Rectangle** choose a
  comment tool for the open document. The chosen tool shows as pressed; choose it again, or the same
  button in the document toolbar, to turn it off. A click places a note; a drag places a highlight or
  a rectangle. Choosing a comment tool turns the Hand tool off.
- **Redact and Apply.** On the **Protect** tab, **Redact** turns the mark tool on or off, the same as
  **Mark areas** on the document toolbar. **Apply** asks you to confirm, then removes the content under
  the marks, as the toolbar's Apply button does. It is disabled when there are no marks to apply.

## Going to a page

- **Page box.** The status bar under the document shows the page you are reading. Type a page
  number and press Enter to go to that page. If the number is not in the document, a message says
  which numbers are valid, and the view does not move.
- **Back and Forward.** The **Back** and **Forward** buttons in the status bar return to pages you
  jumped from, or went to from a thumbnail, and go forward again. A small move of one page is not
  recorded, and a new jump after going back clears the forward pages. The history is kept for each
  open document until you reload the page.
- **Page labels and links.** The status bar shows the document's own page labels (such as i, ii, 1),
  and you can type a label into the page field to go to that page.
- **Links.** Click or tap a link in the document, or Tab to it and press Enter, to jump to the page
  it names; the jump is recorded, so Back returns to where you were. A link to a page one page away
  or on the same page is not recorded in Back. Web and email links ask first: a dialog shows the
  site name and the full address, and nothing opens until you choose **Open link** (Cancel is the
  default). Only web (`http` and `https`) and email (`mailto`) links open; a link to any other
  scheme is blocked with a short message. Links to other files and programs, and any other kind of
  link, do not work in the web version: a short message says so, and nothing is opened.

## Split a document

**Split Document** (Organize tab, Assemble group) makes several PDFs from one. Choose a mode: every
N pages, page ranges, top-level bookmarks, or largest file size. For the first three, the dialog
lists the files it will make, with their page counts, before you split. For the largest file size
the sizes are known only once the files are written, so the list is not shown. Choose **Separate
files** or **One ZIP file**; with more than 10 files, the ZIP is chosen for you. Together the files
can be at most 512 MB. A larger split is refused, and nothing is made.

## What is not available yet

These features are on the desktop today and are planned for the web version. Most of them are not
built in the web version yet:

- the full set of keyboard shortcuts (the ribbon and the command palette work; see
  [Ribbon and keyboard shortcuts](#ribbon-and-keyboard-shortcuts));
- editing the words of a paragraph, adding text and images, and the ribbon's insert, duplicate and extract page commands (moving, resizing and deleting text and images, and the Organize pages panel, are available; see the table below);
- comments other than sticky notes, rectangles and highlights, editing and deleting comments from the Comments panel, stamps and watermarks;
- the fonts, PDF version and security settings in document properties (the title, author, subject and keywords can be edited; see the table below);
- translation (which will be turned on only when you ask for it);
- the **Install App** command.

## Browser support

The target is the current Chrome, Edge, Firefox and Safari on a desktop computer, and Safari on iPad
on a best-effort basis. Testing in every one of these browsers is still to come, so report a problem
if you see one.

## Saving

**Save** (Ctrl+S, the ribbon's Home tab or the quick-access buttons) checks the whole document and
downloads it under the document's own name, so your browser's downloads folder gets the saved file.
Replace the original with it. The browser cannot write over the file you opened, so Save does not
change the original. A download with the same name as an existing file gets a number added by the
browser, so nothing is overwritten.

- After a Save the tab is clean: its dot goes and closing it no longer warns. If the document changed
  while it was being saved, the tab stays unsaved and the app says so.
- A **signed** document is saved as an update to the original, so its signatures stay valid in the
  download. Once you have saved it, a later change cannot be saved in place: Save says so and asks
  you to save it as a new file (the signatures stay in the original). A signed document reopened
  after the engine restarted cannot be saved in place either.
- An **encrypted** document keeps its password and its permissions in the download.
- If a task (such as OCR or a redaction) is still running on the document, Save asks you to wait and
  downloads nothing.
- If the browser blocks the download, the tab stays unsaved and a **Download** button is offered.

**Save As…** (in the app menu, and in the command palette) asks for a file name, prefilled with the
document's name, and downloads a full copy of the document under that name. A blank name gives the
document's own name, and the .pdf ending is added when you leave it out. The tab then takes the new
name, and it is clean, as after a Save. The original file is not changed. A copy of a **signed**
document is a full rewrite, so its signatures are not valid in it; use Save to keep them. A copy of
an encrypted document keeps the password. When Save refuses a signed document, its message offers
**Save as a new file**, which opens Save As.

Writing back to the original file in place needs the browser's File System Access API, and is not
available yet.

## Printing

**Print** (Ctrl+P, or the Home tab's File group) opens the print dialog. Choose the
pages: all pages, the current page, or a range such as `1-3, 5`. Then choose the resolution, 150 dpi
(faster) or 300 dpi (sharper), and whether to include comments, and press **Print**.

- The web version prints each page as an image, not as vector text, so the printed text is as sharp
  as the chosen resolution.
- Printing runs as a job, with its percentage on the job bar. The pages are made one at a time, and
  the browser's print dialog opens once all of them are ready. **Cancel** on the job bar stops the job
  and discards the pages made so far. Nothing in the document changes.
- A print of more than 200 pages asks you to confirm first. Every page is held in memory until the
  browser's print dialog opens, so a long print can slow the tab down. To print less at once, print
  a page range, or choose 150 dpi. **Back** returns to the dialog with your choices kept.
- The dialog and the browser's print dialog are separate: after you press Print, use the browser's
  own options (paper size, copies, the printer) in its dialog.

## Where your data is stored

- **Your documents** are held in the memory of the browser tab while they are open. While a document
  has unsaved changes, the web version also keeps an autosave copy in the browser's private storage
  (not in a folder you can see), about once a minute, so the editor can reopen it if the engine
  restarts. The copy is deleted when you close the document, when it has no unsaved changes, and
  when you leave the page. Password-protected documents are never copied, and a document over 200 MB
  is not copied. If the browser has no such storage, or it is full, the app tells you once and keeps
  working without the copy. A browser crash can leave a copy behind; the app does not read it, and
  clearing the site's data removes it. Save or download a file before you close the tab to keep it.
- **Your theme** is kept in the browser's local storage under the name `elanero.theme`.
- **Your preferences** (language, author name, reduce motion and default zoom) are kept in the same
  browser's local storage under the name `elanero.prefs`. **Clear all local data** in the Preferences
  dialog removes every key that starts with `elanero.`, and the uploaded fonts, but keeps other
  sites' data on the same address and the recovery copies of other open tabs.
- **The app's files** are kept by the browser so the editor works offline.

The web version sends nothing to any server while you edit, and has no accounts, analytics or
advertising. Translation, once it is available, is the only outside request, and it is opt-in. The
full policy, with what the desktop app stores, is in the
[privacy policy](https://github.com/linguagraph/elanero-pdf-editor/blob/main/PRIVACY.md), and
[Privacy](privacy.md) summarizes it.

## Desktop and web: what is available

| Feature | Desktop | Web version |
|---|---|---|
| Light, dark and system theme | Yes | Yes |
| Preferences: language, author name, reduce motion, default zoom, default page layout, redaction box style | Yes | Saved; default zoom and page layout apply now; the author name and redaction style apply to the comments and marks you add next |
| Clear all local data (Preferences, Privacy) | Yes | Removes saved preferences, recent file names and uploaded fonts; disabled while a document is open |
| Install as an app | Windows Store or installer | Yes, with your browser's install option |
| Works offline | Yes | Yes once loaded, for the app and the PDF engine's files; the engine's files come with the web version from its first deployment with them (coming soon) |
| Open, view, search and save PDFs | Yes | Open, view and search work; Save downloads the document under its own name (see Saving), and writing back to the original file in place is coming soon |
| Ribbon, command palette and keyboard shortcuts | Yes | Partly: the ribbon and the command palette (Ctrl+Shift+P) work, with some keys; the full shortcut list is coming soon |
| Select, move, resize and delete text and images (Edit Text & Images) | Yes | Yes, with undo; editing the words of a paragraph is coming soon |
| Add shapes (rectangle, ellipse, line) | Yes | Yes, with undo; style fixed to a black 1.5-point outline |
| Edit the words of a paragraph, add text or images | Yes | Coming soon |
| Insert, duplicate, extract and delete pages | Yes | Yes in the Organize pages panel (see Organize pages); the ribbon's page commands are coming soon |
| Split and combine PDFs | Yes | Yes (Organize tab, Assemble group: Split and Combine; Combine takes page ranges per file, shows each PDF's page count and a result preview, and shows a progress bar, and Cancel stops the job; a password-protected PDF cannot be combined yet; split offers the parts as separate downloads or as one ZIP file) |
| Reduce file size (compress) | Yes | Yes (Tools tab, Optimize group, Reduce File Size; shows both sizes and the space by part before and after, and offers the copy only when it is smaller) |
| Bookmarks: rename, delete, move, set destination | Yes | Yes (Bookmarks panel; Undo after each change; adding bookmarks is coming soon) |
| Comments panel: list, reply and set status | Yes | Yes (Comments button on the left rail; a reply and a status change can be undone; editing and deleting comments from the panel are coming soon) |
| Crop pages (margins in points, millimetres or inches; all, current or chosen pages) | Yes | Yes (Organize tab, Page Setup group, Crop; one Undo after each crop; a signed file keeps its signature, with a note) |
| Crop box drawn on a page | Yes | Yes (Crop dialog, Draw on page: drag a box on a page, Esc cancels; the dialog reopens with its margins on that page) |
| Comments: sticky notes, rectangles and highlights | Yes | Yes (Comment tools in the toolbar; Undo after each) |
| Export comments as XFDF, CSV or PDF summary | Yes | Yes (Comment tab, Manage group, Export Comments; the PDF summary may be unavailable) |
| Import comments from an XFDF file | Yes | Yes (Comment tab, Manage group, Import Comments; files up to 8 MB; Undo after each import) |
| Document properties: title, author, subject, keywords | Yes | Yes (Home tab, File group, Document Properties, Ctrl+D, or the app menu; Undo after each change) |
| Redaction: mark areas, Mark Text, Mark Pages, the Redactions panel, apply | Yes | Yes (Mark areas and Mark Text on the toolbar and the Protect tab; Mark Pages on the Protect tab; the Redactions panel on the left rail; Undo after an apply works until you close the document) |
| Links to pages in the document | Yes | Yes (click, tap, or Tab to the link and press Enter; Back returns) |
| Web and email links | Yes | Yes: web and email links ask first, and show the full address |
| Other comments, stamps, watermarks | Yes | Coming soon |
| Password protection and permissions (Encrypt with Password, Remove Security) | Yes | Yes (command palette; applied when you save; Undo after each change; passwords are not stored) |
| Compare two files (text and appearance) | Yes | Yes (the report saves as a text file, not a PDF) |
| Export pages as text, Markdown, HTML, Word, or images | Yes | Yes (Export, from the Tools group or the command palette) |
| Extract images | Yes | Yes (Tools tab, Export group, Extract Images; choose the pages to look at; several images come back as one ZIP) |
| Extract fonts | Yes | Yes (Tools tab, Export group, Extract Fonts; a font whose licence forbids saving is not saved, and the dialog names it) |
| Print | Yes | Yes (Home tab, File group, Print, or Ctrl+P; choose the page range and resolution, and whether comments are printed) |
| Translation (opt-in) | Yes | Coming soon |
| OCR and batch OCR | Yes | Desktop only (for now) |
| Digital signatures | Yes | Desktop only (for now) |
| PDF/A and accessibility checks | Yes | Desktop only (for now) |

<!--
Evidence for the claims on this page (repository paths, checked on main):
- Web app shell, what is mounted: web/app/src/App.tsx (Ribbon, AppMenu, ThemeSwitch, PreferencesDialog,
  UpdatePrompt, EngineLoadingScreen, ToastRegion).
- Ribbon icons: web/app/src/ribbon/Ribbon.tsx, web/app/src/theme/Icon.tsx, web/app/src/theme/icons.generated.ts
  (from src/pdfeditor/data/icons), web/src/generated/shortcuts.json (desktop icon names).
- Ribbon and app menu: web/app/src/ribbon/Ribbon.tsx, web/app/src/ribbon/AppMenu.tsx,
  web/app/src/commands/run.ts, web/app/src/commands/catalog.ts (seven tabs; app.help group).
- Install App has no handler: web/app/src/commands/catalog.ts ('install-app'); no handler in App.tsx.
- Install (manifest, standalone display): web/app/index.html, web/app/src/pwa/build.ts (buildManifest),
  web/app/vite.config.ts.
- Open with (file_handlers, launch_handler) and the launch queue: web/app/src/pwa/launch.ts,
  web/app/src/pwa/build.ts, web/app/src/main.tsx.
- Offline app files, documents never cached: web/app/public/sw.js, web/app/src/pwa/register.ts,
  web/app/src/main.tsx (service worker registered in production builds only).
- Update banner and Reload: web/app/src/pwa/UpdatePrompt.tsx, web/app/src/pwa/update.ts,
  web/app/src/i18n/en.ts (update.ready, update.reload).
- Theme (system, light, dark; key elanero.theme): web/app/src/theme/theme.ts,
  web/app/src/theme/ThemeSwitch.tsx.
- Preferences dialog and sections (General, Appearance, Viewing; Apply and Cancel; Reduce motion;
  Default zoom): web/app/src/prefs/PreferencesDialog.tsx, web/app/src/i18n/en.ts (prefs.*).
- Preferences store (key elanero.prefs, refuses secret keys, try/catch): web/app/src/prefs/store.ts.
- Preferences opened from the top bar: web/app/src/App.tsx, web/app/src/commands/catalog.ts.
- Autosave copies in the browser's private storage (OPFS): web/app/src/storage/ (opfs.ts,
  autosaveStore.ts, autosaver.ts, tabId.ts); the copy is read from the engine by save.autosave_bytes
  (src/pdfeditor/webapi/save.py). No IndexedDB or file-picker use in web/app/src, web/app/public or
  web/app/index.html (grep); the loader unpacks into the engine's memory file system:
  web/app/src/engine/loader.ts.
- About and source link (AGPL section 13): web/app/src/about.tsx.
- Engine not started yet, loading screen hidden: web/app/src/feedback/LoadingScreen.tsx,
  docs/web/plan.md (W4.4b, W3.1).
- Layout by width: web/app/src/layout/breakpoints.ts, web/app/src/layout/AppFrame.tsx.
- Browser targets, save fallback, File System Access API: docs/web/plan.md ("Other aspects"),
  docs/web/design/W1.6.md.
- Desktop-only features on the web (hidden capabilities ocr, signing, pdfa): docs/web/design/W1.6.md
  section 2.
- Translation is opt-in and the only outside request: PRIVACY.md (web version section), docs/web/plan.md.
- Ribbon tab order and seven tabs: docs/web/design/W1.6.md section 2.
-->
