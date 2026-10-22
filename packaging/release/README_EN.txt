DownloadsOrganizer 1.0.0 — portable version for Windows x64
==============================================================

DownloadsOrganizer sorts the files of one folder (for example, Downloads)
into category folders: PDF, Images, Office, Video, Archive and others.
Before anything is moved it always shows a preview and asks for a
confirmation. Existing files are never overwritten.

Requirements: 64-bit Windows 10 or 11. Python does not need to be
installed. The folders must be on a local NTFS drive.


1. Download and extract
-----------------------
1. Download DownloadsOrganizer-1.0.0-win-x64-portable.zip.
2. Compare the SHA-256 of the archive with the value published with it
   (PowerShell: Get-FileHash .\DownloadsOrganizer-1.0.0-win-x64-portable.zip).
   If they differ, do not run the program.
3. Extract the WHOLE archive to any folder, for example C:\Programs.
   This gives a DownloadsOrganizer folder with DownloadsOrganizer.exe and
   an _internal folder. Do not run the EXE from inside the archive window
   and do not separate DownloadsOrganizer.exe from the _internal folder.

No installation is needed. The program folder can be moved elsewhere as
a whole.


2. Start
--------
Run DownloadsOrganizer.exe.

The program is not digitally signed, so Windows SmartScreen may warn
about an unknown publisher. This warning is not a virus scan. Run the
program only if the archive comes from a source you trust and its
SHA-256 matched. Do not turn off your antivirus or SmartScreen for it.


3. Folders («Sorting» tab)
--------------------------
- «From» — the folder whose files are sorted. Press «Browse…» or paste
  a path.
- «To» — optional. If it is empty, files go to <From>\DEL\<category>.
  If a folder is set, they go to <To>\<category>. «Reset» clears «To».
- The line «Files will be sorted into: …» shows the effective place.
- «To» must be on the same drive as «From»: copying or moving between
  drives is disabled in version 1.0.0.
- Only the files directly in «From» are handled. Subfolders and their
  contents are not touched.


4. File types («File types» tab)
--------------------------------
- At every start no type is selected — tick the ones you need. The type
  selection is not saved between starts.
- Tick extensions in the category cards, a whole category, or press
  «Select all» / «Clear all».
- Rare extensions are behind «▸ N more». Folding a card does not change
  the selection.
- The «Find an extension» field shows the category of an extension.
- With no type selected the preview reports «No file type is selected»
  and nothing is moved.


5. Own extensions
-----------------
1. On the «File types» tab, type an extension in «Own extension:»
   (for example .avif) and choose its category.
2. Press «Add». The new extension starts unticked: tick it to include it
   in sorting. Own extensions are marked «*».
3. Press «Save settings» to keep the rule for the next start. If you
   close the window with unsaved changes, the program asks whether to
   save them.

The standard extensions cannot be changed. Extensions of unfinished
downloads (.part, .crdownload, .tmp and similar) cannot be added.
«Restore the standard set» removes all own extensions.


6. Preview
----------
Press «Preview». It shows which files go where, which get a new name,
and which are skipped and why. The preview changes nothing on the disk.

«If the name is taken:» — what to do when the category folder already
has a file of that name:
- «New name» — the file gets a name like name_1.pdf;
- «Skip» — the file stays where it is.


7. Moving
---------
1. Check the plan and press «Move…».
2. Read the confirmation: the number of files, from where, to where and
   where the journal is written. Press «Move» or «Cancel».
3. «Cancel» during moving stops it after the current file.

Files are MOVED, not copied: they will no longer be in the source
folder. There is NO automatic undo (rollback) of a completed move.
Files can only be moved back by hand — the journal records the «from»
and «to» path of every file.

A file that changed after the preview is not moved and stays in place.
Press «Refresh the preview of the remaining files», check the new plan
and confirm the move again.


8. Why a file can be skipped
----------------------------
- its type is not selected or its extension is unknown («outside the
  plan»);
- it is a hidden or system file;
- it is a link, a junction or another reparse point;
- it is a cloud file (for example OneDrive) that is not on the disk;
- it is an unfinished download (.part, .crdownload, ...) or its
  unfinished part lies next to it;
- the destination already has a file of that name and «Skip» is chosen;
- it is in use by another program or changed after the preview;
- access is denied, the path is too long, or the file could not be
  checked.
The reason is shown in the preview and in the results. Skipped files
stay where they are.


9. Where settings and journals are kept
---------------------------------------
- Settings (language and own extensions):
  %LOCALAPPDATA%\DownloadsOrganizer\settings.json
  The file is written only by «Save settings» (or «Save» when the window
  is closed).
- Move journals:
  %LOCALAPPDATA%\DownloadsOrganizer\logs\*.jsonl
  A journal is created only by a move. It can be viewed on the
  «Journals» tab. The program never deletes or cleans up journals.

Nothing is written into the program folder. Starting the program or
making a preview creates no journal and moves no file.

The interface language is switched by the RU/EN button to the right of
the «From» field. «Save settings» keeps the chosen language.


10. Removing the program
------------------------
1. Close DownloadsOrganizer.
2. Delete the DownloadsOrganizer program folder.
3. To remove the settings and journals, delete the folder
   %LOCALAPPDATA%\DownloadsOrganizer
   (paste this path into the File Explorer address bar).
   The journals are the only record of where files were moved. Keep
   them if you may need them.

Moved files stay where they were moved when the program is removed.


11. Limitations of version 1.0.0
--------------------------------
- Moves within one drive only; copying between drives is disabled.
- Local NTFS drives only; network drives, FAT32 and exFAT are not
  supported.
- No subfolder traversal, no moving of folders.
- No automatic undo of completed moves.
- The program is not digitally signed.
See RELEASE_NOTES.txt for details. Third-party components and their
licenses: THIRD_PARTY_NOTICES.txt and the licenses folder.


12. Single-file EXE
-------------------
Besides the portable archive there is a single-file EXE:
DownloadsOrganizer-1.0.0-win-x64-onefile.exe. The portable archive is
the main option. At every start the single-file EXE extracts its
components to the Windows temporary folder (%TEMP%). Its settings and
journals are kept in the same place as those of the portable version
(section 9). The licenses of this EXE are in
DownloadsOrganizer-1.0.0-win-x64-onefile-licenses.zip; distribute the
EXE only together with that archive.


13. License
-----------
DownloadsOrganizer is distributed under the MIT License
(Copyright (c) 2026 Omaeon); the text is in LICENSE.txt.
- The program may be used free of charge, commercial use included.
- The source code may be modified and distributed, modified versions
  included.
- The copyright notice and the MIT License text must be kept in all
  copies or substantial portions of the program.
- The program is provided «as is», without warranty of any kind (see
  LICENSE.txt).
The MIT License covers only DownloadsOrganizer's own code. Third-party
components (Python, Tcl/Tk, CustomTkinter and others) are distributed
under their own licenses — see THIRD_PARTY_NOTICES.txt and the licenses
folder.
