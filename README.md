# DownloadsOrganizer

**A practical Windows tool for sorting the files that accumulate in your Downloads folder.**

[Русская версия](README_RU.md) · [Download v1.0.0](https://github.com/Omaeon/DownloadsOrganizer/releases/tag/v1.0.0) · [MIT License](LICENSE)

## Why this project exists

Almost everyone has a Downloads folder that slowly fills up with documents, archives, images, installers, and files with names that are hard to recognize. Finding something you downloaded last week becomes increasingly difficult.

**DownloadsOrganizer** was created to make this routine task simpler: select a folder, review a proposed sorting plan, and explicitly approve moving files into categories. The goal is a clear interface with predictable behavior—not an automatic process that silently rearranges your data.

The project also explores an **AI-driven software development workflow**: requirements and release decisions were coordinated by the project owner with ChatGPT, implementation was performed through Claude Code, and independent code reviews were conducted with Codex CLI.

## Features

- **Preview before moving:** see the planned destinations and results before committing changes.
- **Explicit confirmation:** file movement does not start merely because a folder was selected or previewed.
- **File-type organization:** predefined types and categories, with the ability to configure custom extensions.
- **Filename conflicts:** choose between generating a new name and skipping a conflicting file. Existing destination files are not intentionally overwritten.
- **Changed-file protection:** files whose metadata changes after preview are skipped; refresh the preview before trying again.
- **Execution journals:** inspect results, skipped files, and reasons in the application.
- **Russian and English interface.**
- **Portable Windows distribution:** download, extract, and run without installing Python.

## Download and run

**Platform:** Windows x64. The first published package is a portable ZIP; there is no installer.

1. Open the [v1.0.0 release page](https://github.com/Omaeon/DownloadsOrganizer/releases/tag/v1.0.0).
2. Download `DownloadsOrganizer-1.0.0-win-x64-portable.zip` and `SHA256SUMS.txt`.
3. Extract the ZIP to a short, writable path such as `C:\Apps\DownloadsOrganizer`. Keep the `DownloadsOrganizer.exe` file and `_internal` folder together.
4. Run `DownloadsOrganizer.exe`.

Python does **not** need to be installed. The executable is currently **unsigned**; Windows may show a reputation/security warning. Do not disable antivirus or Windows security protections to run it.

### Check the download

In PowerShell, from the directory containing the downloaded ZIP:

```powershell
Get-FileHash .\DownloadsOrganizer-1.0.0-win-x64-portable.zip -Algorithm SHA256
```

For the original v1.0.0 archive, the expected SHA-256 is:

```text
9f93dbb6d9faeade3dd73bf9822f01949e36ce3168cb011aa575fc0e0a5d6d88
```

Compare the entire value with the published checksum. A different hash means the file is not byte-for-byte identical to the verified release archive.

## How to organize files

1. In **From**, select the folder you want to organize.
2. In **To**, optionally choose a destination. If left empty, the application shows a default destination under the source folder in a `DEL` subfolder, organized by category.
3. Open **File types** and select the types you want to process. If none are selected, moving is blocked.
4. Choose the filename-conflict behavior: **New name** or **Skip**.
5. Click **Preview** and inspect the planned actions and destination paths.
6. Click **Move**, review the confirmation, and explicitly approve.
7. Check the results and **Journals**. If a file was skipped because it changed after preview, refresh the preview and review the new plan.

Example of the default layout (illustrative):

```text
Downloads/
├── report.pdf
├── picture.jpg
└── DEL/
    ├── PDF/report.pdf
    └── Images/picture.jpg
```

Custom settings are saved through the application's explicit **Save settings** action. The current version does **not** persist the selection of file types between sessions; check the selection again after reopening the program.

## Safety and limitations

Read these points before organizing important files:

- **v1.0.0 supports MOVE on the same volume only.** Cross-volume COPY remains disabled.
- Every move requires a valid preview followed by user confirmation.
- The application checks whether a planned source file has changed; such files may be skipped rather than moved.
- Filename collisions can be renamed or skipped; the application does not deliberately replace an existing destination file.
- **Completed moves are not automatically rolled back.** There is no automatic undo or recovery procedure. Check your preview and maintain backups of important data.
- Some files may be excluded because their types are not selected or do not match configured rules.
- The package is portable **to launch**, but settings and journals are stored in `%LOCALAPPDATA%\DownloadsOrganizer`, not beside the EXE.
- Very long extraction paths can affect CustomTkinter font loading; a short extraction path is recommended.
- Testing was performed on a developer Windows environment at 125% display scaling. A clean Windows installation, Windows 10, and 100%/150% scaling were not separately validated for this release.

## An AI-driven development experiment

One notable aspect of DownloadsOrganizer is the workflow used to build it. The project owner defined the requirements, set scope and safety boundaries, and approved stage completions. AI assistants performed much of the coordination, coding, and review work.

| Role | Tool / model configuration reported for the project |
|---|---|
| Coordinator | **ChatGPT — GPT-6 High** |
| Developer | **Claude Code — Opus 5.5 High** |
| Independent code audit | **Codex CLI — GPT-6.1-Sol Medium** |

**Development loop:**

```text
Project owner / requirements
            ↓
ChatGPT (planning, scope, acceptance criteria)
            ↓
Claude Code (implementation and tests)
            ↓
Codex CLI (independent read-only audit)
            ↓
ChatGPT (findings, acceptance, next step)
            ↺ targeted changes when necessary
```

To avoid endless review cycles, the project used a **maximum of three targeted correction iterations per stage** as a working rule:

- **High:** up to two corrective iterations.
- **Medium:** up to one corrective iteration.
- **Low:** documented without a mandatory extra iteration.

Confirmed Critical/High issues in the active release scope were treated as blockers, not waived because an iteration budget was exhausted. The model labels above reflect the project owner's development notes; they are not independently verified performance claims.

The aim was not to eliminate human responsibility, but to see how far a structured **AI coordination → implementation → independent audit → decision** workflow could take a real Windows utility.

## Source code and development

The source code is published for inspection, learning, modification, and reuse under the MIT License. The project uses Python, CustomTkinter, and PyInstaller for the Windows distribution. Dependencies are pinned in `pyproject.toml` and `uv.lock` (Python 3.14); a development environment can be created with `uv sync`.

The automated tests can be run from an appropriately configured development environment using `python -m pytest`. Packaging depends on the Windows toolchain and project configuration.

If you discover a problem, open a [GitHub Issue](https://github.com/Omaeon/DownloadsOrganizer/issues) with the version, Windows version, steps to reproduce, and a description of what happened. **Remove personal file names, paths, and journal content before posting.**

## License and third-party notices

**DownloadsOrganizer's own code: MIT License — Copyright (c) 2026 Omaeon.** See [LICENSE](LICENSE).

Bundled third-party libraries, fonts, and other components retain their own licenses. The portable download includes `THIRD_PARTY_NOTICES.txt` and the relevant `licenses/` directory. If distributing the optional onefile EXE separately, also include its matching `onefile-licenses.zip` package.

---

*DownloadsOrganizer 1.0.0 — a small utility and an experiment in disciplined AI-assisted engineering.*
