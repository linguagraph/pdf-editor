# Pages

Page commands change the file's pages: their order, their number and their content. They are on the
**Organize** ribbon tab and in the **Pages** menu. Every change can be undone with **Ctrl+Z**.

![The Organize ribbon with page, assembly, page setup and marks tools](images/elanero-02-organize.png)

## Organize pages visually

**Pages ▸ Organize Pages** (**Ctrl+Shift+O**) shows all pages as a grid of
thumbnails.

- Select one page, or several with **Ctrl+click** and **Shift+click**.
- Drag pages to new positions to reorder them.
- Use the buttons of the **Pages** group, described below, to rotate, duplicate or delete the
  selected pages.

## Insert, delete, duplicate and rotate

The **Pages** group works on the selected pages (in the page view, the current page):

- **Insert Pages…** (**Ctrl+Shift+I**): choose **Blank page**, **From file** (pages of another PDF
  or an image), or **Image from clipboard**, and whether to insert before or after the current page.
- **Delete Pages** (**Ctrl+Shift+D**): removes the selected pages.
- **Duplicate Pages**: adds a copy of each selected page.
- **Rotate Pages Left** (**Ctrl+Shift+Left**) and **Rotate Pages Right** (**Ctrl+Shift+Right**) turn
  the pages in the file by 90 degrees.

Rotating pages changes the file. To turn pages on screen only, use
[Reading and navigation](reading.md#rotate-the-view).

## Extract, replace and split

The **Assemble** group:

- **Extract Pages…** copies the chosen pages into a new PDF. Choose **Delete pages after extracting**
  to remove them from the open document, or **Extract each page as a separate file** to get one file
  per page.
- **Replace Pages…** replaces pages of the open document with pages from another PDF.
- **Split Document…** writes several files. Split **every** N pages, into **page ranges** (one file
  per range, separated by `;`), **at each top-level bookmark**, or into files of a **maximum file
  size**. Choose the output folder and a file name prefix. A part with the same name as an earlier
  one gets " (2)", " (3)" and so on, so no part overwrites another.
- **Combine Files into PDF…** joins several files into one. See below.

## Combine files

**Combine Files into PDF…** opens the combine panel. Add PDFs and images (PNG, JPEG and TIFF); they
are joined in the order shown in the list. You can:

- Look at each file's pages before you combine.
- Reorder the files and choose page ranges for each.
- Enter the password of a file that is protected, so its pages can be used.
- Keep the bookmarks of each file, merged into one outline.

To use only some pages of a PDF, type them in its **Pages** box. Use commas between items and
dashes for spans: `1-3, 5` takes pages 1 to 3 and page 5, `8-` takes page 8 to the end, and `-4`
takes pages 1 to 4. `all`, or an empty box, takes every page, and `;` works like `,`. The pages
keep their order in the document, whatever order you type them in, so `5, 1` gives page 1 then
page 5. A page number above the file's last page is refused, and the row says how many pages the
file has. A bookmark of a page you leave out is dropped, and its sub-bookmarks move up one level.
The web version takes the same ranges. Each PDF row there shows its page count and where its pages
land in the combined file (for example "pages 4–6 of the result"), and a total under the list says how
many pages the combined file will have. A PDF larger than 64 MB is counted when you combine it. A
password-protected PDF cannot be combined on the web yet: remove it from the list.

The **Quick actions** on the start page also start this panel, as **Combine files**.

## Crop and page labels

The **Page Setup** group:

- **Crop Pages…** (**Ctrl+Shift+T**) trims margins by the amounts you enter, or removes white
  margins automatically, for the page ranges you choose. Cropping hides the margins from view and
  print; the content stays in the file. Margins that would leave nothing of a page are refused.
  In the web version, **Draw on page** in the dialog lets you drag a box on a page instead: the
  dialog then reopens with the margins that box leaves, on that page. Press **Esc** to stop drawing
  without a box; the dialog reopens as it was.
- **Page Labels…** sets how pages are numbered in the navigator, the thumbnails and the print dialog.
  Each rule starts at a page and gives a numbering style (or none, for a prefix only), an optional
  prefix and the number to start at. For example, a front matter numbered `i, ii, iii` followed by
  `1, 2, 3`.

## Headers, footers, Bates numbers, watermarks and backgrounds

The **Marks** group adds text or images to pages. Each button opens a dialog when you click it. Its
arrow offers **Add**, **Update** and **Remove**, so you can change or take off an existing mark.

- **Header & Footer**: text at the top or bottom of the pages, left, center or right. Fields are
  replaced for each page: `<<page>>`, `<<pages>>`, `<<label>>`, `<<date>>`, `<<file>>` and
  `<<bates>>`.
- **Bates Numbering**: sequential legal numbers on every page, with a prefix and a number of digits.
- **Watermark**: text (for example `CONFIDENTIAL`) or an image across the pages, with font and
  opacity settings.
- **Background**: a color or an image behind the page content.

Most of these dialogs let you choose which pages a mark applies to: all pages, the current page
or a custom range.

!!! note
    The marks are drawn into the page content and saved with the file. To change one later, use
    its **Update** command; to take it off, use **Remove**.

## Limits

- Pages that are damaged or use unusual features may not be handled by every command. Elanero
  reports a failure rather than writing a broken file.
- Deleting a page removes it from the file. To remove sensitive content from every version of the
  file, use [Redaction and security](security.md).
