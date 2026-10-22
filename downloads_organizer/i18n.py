"""Russian and English texts of the window (R4-C).

Only what the user reads is translated. Reason codes, journal records (JSONL), the
settings file and the category folder names on disk are never translated: they stay the
codes and names the program works with. A code without a text is shown as it is.

* ``MESSAGES`` is every fixed sentence: a key and its Russian and English text, with the
  same ``{placeholders}`` in both (``tr``).
* The ``*_TEXT`` tables give each code of an enum its text in each language (``pick``).
"""

from __future__ import annotations

import string
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from downloads_organizer.executor import ExecReason, ItemStatus
from downloads_organizer.extensions import MAX_EXTENSION_LENGTH, ExtensionErrorReason
from downloads_organizer.journal import OperationState
from downloads_organizer.journal_v2 import ReviewFlag
from downloads_organizer.models import PlanAction, PlanErrorReason, SkipReason, VolumeMode
from downloads_organizer.runner import RunRefusalReason


class Lang(StrEnum):
    RU = "ru"
    EN = "en"


DEFAULT_LANG = Lang.RU
LANG_NAMES: Mapping[Lang, str] = MappingProxyType({Lang.RU: "Русский", Lang.EN: "English"})


def _table[K](ru: Mapping[K, str], en: Mapping[K, str]) -> Mapping[Lang, Mapping[K, str]]:
    return MappingProxyType(
        {Lang.RU: MappingProxyType(dict(ru)), Lang.EN: MappingProxyType(dict(en))}
    )


def pick[K](table: Mapping[Lang, Mapping[K, str]], code: K, lang: Lang) -> str:
    """The text of ``code`` in ``lang``; the code itself if it has none."""
    return table[Lang(lang)].get(code, str(code))


# --- codes -------------------------------------------------------------------------------

CATEGORY_TEXT = _table(
    {
        "Torrent": "Торренты",
        "PDF": "PDF",
        "Images": "Изображения",
        "Office": "Документы Office",
        "EXE": "Программы",
        "Video": "Видео",
        "TXT": "Текст",
        "Archive": "Архивы",
        "Audio": "Аудио",
        "Code": "Код",
    },
    {
        "Torrent": "Torrents",
        "PDF": "PDF",
        "Images": "Images",
        "Office": "Office documents",
        "EXE": "Programs",
        "Video": "Video",
        "TXT": "Text",
        "Archive": "Archives",
        "Audio": "Audio",
        "Code": "Code",
    },
)

PLAN_ERROR_TEXT = _table(
    {
        PlanErrorReason.NOTHING_SELECTED: "Не выбрано ни одного типа файлов",
        PlanErrorReason.SOURCE_INVALID_PATH: "Недопустимый путь исходной папки",
        PlanErrorReason.SOURCE_MISSING: "Исходная папка не найдена",
        PlanErrorReason.SOURCE_NOT_DIRECTORY: "Исходный путь — не папка",
        PlanErrorReason.SOURCE_REPARSE_POINT: (
            "Исходная папка — ссылка, junction или точка монтирования"
        ),
        PlanErrorReason.SOURCE_ACCESS_DENIED: "Нет доступа к исходной папке",
        PlanErrorReason.SOURCE_UNREADABLE: "Не удалось прочитать исходную папку",
        PlanErrorReason.SOURCE_METADATA_UNKNOWN: "Не удалось определить свойства исходной папки",
        PlanErrorReason.DESTINATION_INVALID_PATH: "Недопустимый путь папки назначения",
        PlanErrorReason.DESTINATION_UNAVAILABLE: "Папка назначения недоступна",
        PlanErrorReason.DESTINATION_NOT_DIRECTORY: "Путь назначения — не папка",
        PlanErrorReason.DESTINATION_REPARSE_POINT: (
            "Путь назначения содержит ссылку, junction или точку монтирования"
        ),
        PlanErrorReason.DESTINATION_ACCESS_DENIED: "Нет доступа к папке назначения",
        PlanErrorReason.DESTINATION_METADATA_UNKNOWN: (
            "Не удалось определить свойства папки назначения"
        ),
        PlanErrorReason.SAME_DIRECTORY: "Папка назначения совпадает с исходной",
        PlanErrorReason.DIRECTORY_IDENTITY_UNKNOWN: (
            "Не удалось доказать, что папка назначения отличается от исходной"
        ),
        PlanErrorReason.VOLUME_UNVERIFIED: (
            "Диск не подтверждён: поддерживаются только локальные тома NTFS"
        ),
        PlanErrorReason.CROSS_VOLUME: (
            "Копирование между томами пока отключено: выберите папку назначения"
            " на том же диске, что и исходная"
        ),
        PlanErrorReason.VOLUME_AMBIGUOUS: (
            "Тома источника и назначения не удалось однозначно сопоставить"
        ),
    },
    {
        PlanErrorReason.NOTHING_SELECTED: "No file type is selected",
        PlanErrorReason.SOURCE_INVALID_PATH: "The source folder path is not valid",
        PlanErrorReason.SOURCE_MISSING: "The source folder was not found",
        PlanErrorReason.SOURCE_NOT_DIRECTORY: "The source path is not a folder",
        PlanErrorReason.SOURCE_REPARSE_POINT: (
            "The source folder is a link, a junction or a mount point"
        ),
        PlanErrorReason.SOURCE_ACCESS_DENIED: "Access to the source folder is denied",
        PlanErrorReason.SOURCE_UNREADABLE: "The source folder could not be read",
        PlanErrorReason.SOURCE_METADATA_UNKNOWN: (
            "The properties of the source folder could not be determined"
        ),
        PlanErrorReason.DESTINATION_INVALID_PATH: "The destination folder path is not valid",
        PlanErrorReason.DESTINATION_UNAVAILABLE: "The destination folder is not available",
        PlanErrorReason.DESTINATION_NOT_DIRECTORY: "The destination path is not a folder",
        PlanErrorReason.DESTINATION_REPARSE_POINT: (
            "The destination path contains a link, a junction or a mount point"
        ),
        PlanErrorReason.DESTINATION_ACCESS_DENIED: "Access to the destination folder is denied",
        PlanErrorReason.DESTINATION_METADATA_UNKNOWN: (
            "The properties of the destination folder could not be determined"
        ),
        PlanErrorReason.SAME_DIRECTORY: "The destination folder is the source folder",
        PlanErrorReason.DIRECTORY_IDENTITY_UNKNOWN: (
            "It could not be proven that the destination folder differs from the source"
        ),
        PlanErrorReason.VOLUME_UNVERIFIED: (
            "The drive is not verified: only local NTFS volumes are supported"
        ),
        PlanErrorReason.CROSS_VOLUME: (
            "Copying between volumes is disabled for now: choose a destination folder"
            " on the same drive as the source"
        ),
        PlanErrorReason.VOLUME_AMBIGUOUS: (
            "The source and destination volumes could not be matched unambiguously"
        ),
    },
)

SKIP_TEXT = _table(
    {
        SkipReason.SYMLINK: "Символическая ссылка — не перемещается",
        SkipReason.CLOUD_PLACEHOLDER: "Облачный файл, не загруженный на диск",
        SkipReason.OFFLINE: "Файл помечен как автономный (offline)",
        SkipReason.REPARSE_POINT: "Ссылка или точка повторного анализа",
        SkipReason.NOT_REGULAR_FILE: "Не обычный файл",
        SkipReason.SYSTEM: "Системный файл",
        SkipReason.HIDDEN: "Скрытый файл",
        SkipReason.METADATA_UNKNOWN: "Не удалось прочитать свойства файла",
        SkipReason.PARTIAL_DOWNLOAD: "Незавершённая загрузка",
        SkipReason.VANISHED: "Файл исчез во время проверки",
        SkipReason.STAT_FAILED: "Не удалось получить сведения о файле",
        SkipReason.TARGET_DIR_INVALID: "Папка категории недопустима (файл или ссылка)",
        SkipReason.TARGET_DIR_IS_SOURCE: "Папка категории совпадает с исходной",
        SkipReason.TARGET_DIR_IDENTITY_UNKNOWN: (
            "Не удалось доказать, что папка категории не исходная"
        ),
        SkipReason.TARGET_CHECK_FAILED: "Не удалось проверить место назначения",
        SkipReason.TARGET_EXISTS: "Файл с таким именем уже есть в назначении",
        SkipReason.TARGET_RESERVED: "Имя назначения занято другим файлом этого плана",
        SkipReason.NO_FREE_NAME: "Не найдено свободное имя",
        SkipReason.NAME_TOO_LONG: "Слишком длинный путь назначения",
    },
    {
        SkipReason.SYMLINK: "Symbolic link: not moved",
        SkipReason.CLOUD_PLACEHOLDER: "Cloud file that is not downloaded to the disk",
        SkipReason.OFFLINE: "The file is marked offline",
        SkipReason.REPARSE_POINT: "Link or reparse point",
        SkipReason.NOT_REGULAR_FILE: "Not a regular file",
        SkipReason.SYSTEM: "System file",
        SkipReason.HIDDEN: "Hidden file",
        SkipReason.METADATA_UNKNOWN: "The file properties could not be read",
        SkipReason.PARTIAL_DOWNLOAD: "Unfinished download",
        SkipReason.VANISHED: "The file disappeared during the check",
        SkipReason.STAT_FAILED: "The file information could not be read",
        SkipReason.TARGET_DIR_INVALID: "The category folder is not valid (a file or a link)",
        SkipReason.TARGET_DIR_IS_SOURCE: "The category folder is the source folder",
        SkipReason.TARGET_DIR_IDENTITY_UNKNOWN: (
            "It could not be proven that the category folder is not the source"
        ),
        SkipReason.TARGET_CHECK_FAILED: "The destination could not be checked",
        SkipReason.TARGET_EXISTS: "A file with this name already exists in the destination",
        SkipReason.TARGET_RESERVED: "The target name is taken by another file of this plan",
        SkipReason.NO_FREE_NAME: "No free name was found",
        SkipReason.NAME_TOO_LONG: "The destination path is too long",
    },
)

ACTION_TEXT = _table(
    {
        PlanAction.MOVE: "Переместить",
        PlanAction.RENAME: "Переместить с новым именем",
        PlanAction.SKIP: "Пропустить",
    },
    {
        PlanAction.MOVE: "Move",
        PlanAction.RENAME: "Move with a new name",
        PlanAction.SKIP: "Skip",
    },
)

ITEM_STATUS_TEXT = _table(
    {
        ItemStatus.MOVED: "Перемещено",
        ItemStatus.SKIPPED: "Пропущено",
        ItemStatus.FAILED: "Ошибка",
        ItemStatus.CANCELLED: "Отменено",
        ItemStatus.RECOVERY_REQUIRED: "Требует ручной проверки",
    },
    {
        ItemStatus.MOVED: "Moved",
        ItemStatus.SKIPPED: "Skipped",
        ItemStatus.FAILED: "Error",
        ItemStatus.CANCELLED: "Cancelled",
        ItemStatus.RECOVERY_REQUIRED: "Needs a manual check",
    },
)

EXEC_TEXT = _table(
    {
        ExecReason.COMPLETED: "Выполнено",
        ExecReason.PLANNED_SKIP: "Пропущено по плану",
        ExecReason.PLAN_INVALID: "План недействителен",
        ExecReason.UNSUPPORTED_PLATFORM: "Поддерживается только Windows",
        ExecReason.UNSAFE_NAME: "Небезопасное имя",
        ExecReason.METADATA_UNKNOWN: "Свойства файла неизвестны",
        ExecReason.SOURCE_MISSING: "Файл исчез после предпросмотра",
        ExecReason.SOURCE_CHANGED: (
            "Файл изменился после предпросмотра. Перемещение отменено для безопасности"
        ),
        ExecReason.SOURCE_REPLACED: "Файл заменён другим после предпросмотра",
        ExecReason.SOURCE_UNSAFE: "Файл стал небезопасным (ссылка, облачный, скрытый, системный)",
        ExecReason.SOURCE_IN_USE: "Файл занят другой программой",
        ExecReason.SOURCE_CHECK_FAILED: "Не удалось проверить файл",
        ExecReason.SOURCE_DIR_CHANGED: "Исходная папка изменилась",
        ExecReason.DESTINATION_CHANGED: "Папка назначения изменилась",
        ExecReason.TARGET_DIR_MISSING: "Папка категории отсутствует",
        ExecReason.TARGET_DIR_INVALID: "Папка категории недопустима",
        ExecReason.TARGET_DIR_IS_SOURCE: "Папка категории совпадает с исходной",
        ExecReason.TARGET_DIR_FAILED: "Не удалось создать или открыть папку категории",
        ExecReason.TARGET_EXISTS: "В назначении появился файл с этим именем; он не тронут",
        ExecReason.VOLUME_UNKNOWN: "Том не определён",
        ExecReason.ROOT_UNVERIFIED: "Диск не подтверждён как локальный том NTFS",
        ExecReason.CROSS_VOLUME: "Другой том: перемещение между томами не выполняется",
        ExecReason.ACCESS_DENIED: "Отказано в доступе",
        ExecReason.MOVE_FAILED: "Перемещение не удалось",
        ExecReason.VERIFY_FAILED: "Не удалось подтвердить результат перемещения",
        ExecReason.JOURNAL_FAILED: "Ошибка журнала: дальнейшие изменения остановлены",
        ExecReason.CANCELLED: "Отменено",
        ExecReason.RUN_ABORTED: "Выполнение остановлено из-за непредвиденной ошибки",
        ExecReason.CLEANUP_FAILED: "Не удалось освободить системный ресурс",
        ExecReason.UNEXPECTED_ERROR: "Непредвиденная ошибка",
    },
    {
        ExecReason.COMPLETED: "Done",
        ExecReason.PLANNED_SKIP: "Skipped by the plan",
        ExecReason.PLAN_INVALID: "The plan is not valid",
        ExecReason.UNSUPPORTED_PLATFORM: "Only Windows is supported",
        ExecReason.UNSAFE_NAME: "Unsafe name",
        ExecReason.METADATA_UNKNOWN: "The file properties are unknown",
        ExecReason.SOURCE_MISSING: "The file disappeared after the preview",
        ExecReason.SOURCE_CHANGED: (
            "The file changed after the preview. It was not moved, for safety"
        ),
        ExecReason.SOURCE_REPLACED: "The file was replaced by another one after the preview",
        ExecReason.SOURCE_UNSAFE: "The file became unsafe (a link, cloud, hidden or system file)",
        ExecReason.SOURCE_IN_USE: "The file is in use by another program",
        ExecReason.SOURCE_CHECK_FAILED: "The file could not be checked",
        ExecReason.SOURCE_DIR_CHANGED: "The source folder changed",
        ExecReason.DESTINATION_CHANGED: "The destination folder changed",
        ExecReason.TARGET_DIR_MISSING: "The category folder is missing",
        ExecReason.TARGET_DIR_INVALID: "The category folder is not valid",
        ExecReason.TARGET_DIR_IS_SOURCE: "The category folder is the source folder",
        ExecReason.TARGET_DIR_FAILED: "The category folder could not be created or opened",
        ExecReason.TARGET_EXISTS: (
            "A file with this name appeared in the destination; it was not touched"
        ),
        ExecReason.VOLUME_UNKNOWN: "The volume is unknown",
        ExecReason.ROOT_UNVERIFIED: "The drive is not verified as a local NTFS volume",
        ExecReason.CROSS_VOLUME: "Another volume: moves between volumes are not made",
        ExecReason.ACCESS_DENIED: "Access denied",
        ExecReason.MOVE_FAILED: "The move failed",
        ExecReason.VERIFY_FAILED: "The result of the move could not be confirmed",
        ExecReason.JOURNAL_FAILED: "Journal error: further changes were stopped",
        ExecReason.CANCELLED: "Cancelled",
        ExecReason.RUN_ABORTED: "The run was stopped by an unexpected error",
        ExecReason.CLEANUP_FAILED: "A system resource could not be released",
        ExecReason.UNEXPECTED_ERROR: "Unexpected error",
    },
)

RUN_REFUSAL_TEXT = _table(
    {
        RunRefusalReason.PLAN_INVALID: "План недействителен",
        RunRefusalReason.NO_VOLUME_PROOF: "План не прошёл проверку тома",
        RunRefusalReason.VOLUME_REFUSED: "Тома источника и назначения не допускают выполнение",
        RunRefusalReason.COPY_DISABLED: "Копирование между томами пока отключено",
        RunRefusalReason.UNSUPPORTED_PLATFORM: "Поддерживается только Windows",
        RunRefusalReason.ACL_UNDECIDED: "Политика прав доступа для копирования не утверждена",
        RunRefusalReason.PLAN_STALE: (
            "Папки изменились после предпросмотра: выполните предпросмотр заново"
        ),
        RunRefusalReason.JOURNAL_FAILED: "Не удалось создать журнал",
    },
    {
        RunRefusalReason.PLAN_INVALID: "The plan is not valid",
        RunRefusalReason.NO_VOLUME_PROOF: "The plan did not pass the volume check",
        RunRefusalReason.VOLUME_REFUSED: "The source and destination volumes do not allow a run",
        RunRefusalReason.COPY_DISABLED: "Copying between volumes is disabled for now",
        RunRefusalReason.UNSUPPORTED_PLATFORM: "Only Windows is supported",
        RunRefusalReason.ACL_UNDECIDED: "The access rights policy for copying is not approved",
        RunRefusalReason.PLAN_STALE: ("The folders changed after the preview: make a new preview"),
        RunRefusalReason.JOURNAL_FAILED: "The journal could not be created",
    },
)

STATE_TEXT = _table(
    {
        OperationState.UNCERTAIN: "Итог неизвестен (начато, результат не записан)",
        OperationState.DONE: "Выполнено",
        OperationState.SKIPPED: "Пропущено",
        OperationState.FAILED: "Ошибка",
        OperationState.CANCELLED: "Отменено",
        OperationState.RECOVERY_REQUIRED: "Требует ручной проверки",
    },
    {
        OperationState.UNCERTAIN: "Outcome unknown (started, no result recorded)",
        OperationState.DONE: "Done",
        OperationState.SKIPPED: "Skipped",
        OperationState.FAILED: "Error",
        OperationState.CANCELLED: "Cancelled",
        OperationState.RECOVERY_REQUIRED: "Needs a manual check",
    },
)

FLAG_TEXT = _table(
    {
        ReviewFlag.TEMP_MAY_EXIST: "временный файл мог быть создан",
        ReviewFlag.TEMP_LEFT: "временный файл мог остаться",
        ReviewFlag.COMMIT_UNKNOWN: "неизвестно, создан ли итоговый файл",
    },
    {
        ReviewFlag.TEMP_MAY_EXIST: "a temporary file may have been created",
        ReviewFlag.TEMP_LEFT: "a temporary file may be left",
        ReviewFlag.COMMIT_UNKNOWN: "it is unknown whether the final file was created",
    },
)

EXTENSION_ERROR_TEXT = _table(
    {
        ExtensionErrorReason.EMPTY: "Пустое расширение",
        ExtensionErrorReason.INVALID_CHARACTERS: (
            "Допустимы только латинские буквы, цифры, «_» и «-»"
        ),
        ExtensionErrorReason.COMPOUND: (
            "Составные расширения (например, .tar.gz) не поддерживаются"
        ),
        ExtensionErrorReason.TOO_LONG: (
            f"Расширение длиннее {MAX_EXTENSION_LENGTH} символов вместе с точкой"
        ),
        ExtensionErrorReason.PARTIAL_DOWNLOAD: "Это расширение незавершённой загрузки",
        ExtensionErrorReason.BUILTIN_DUPLICATE: "Расширение уже есть во встроенной категории",
        ExtensionErrorReason.CUSTOM_DUPLICATE: "Расширение уже добавлено",
        ExtensionErrorReason.AMBIGUOUS: "Расширение назначено нескольким категориям",
        ExtensionErrorReason.UNKNOWN_CATEGORY: "Неизвестная категория",
        ExtensionErrorReason.NOT_CUSTOM: "Это не пользовательское расширение",
        ExtensionErrorReason.MALFORMED: "Некорректная запись",
    },
    {
        ExtensionErrorReason.EMPTY: "Empty extension",
        ExtensionErrorReason.INVALID_CHARACTERS: (
            "Only Latin letters, digits, «_» and «-» are allowed"
        ),
        ExtensionErrorReason.COMPOUND: "Compound extensions (such as .tar.gz) are not supported",
        ExtensionErrorReason.TOO_LONG: (
            f"The extension is longer than {MAX_EXTENSION_LENGTH} characters with the dot"
        ),
        ExtensionErrorReason.PARTIAL_DOWNLOAD: "This is an extension of unfinished downloads",
        ExtensionErrorReason.BUILTIN_DUPLICATE: "The extension is already in a built-in category",
        ExtensionErrorReason.CUSTOM_DUPLICATE: "The extension is already added",
        ExtensionErrorReason.AMBIGUOUS: "The extension is assigned to several categories",
        ExtensionErrorReason.UNKNOWN_CATEGORY: "Unknown category",
        ExtensionErrorReason.NOT_CUSTOM: "This is not a custom extension",
        ExtensionErrorReason.MALFORMED: "Malformed entry",
    },
)

VOLUME_MODE_TEXT = _table(
    {
        VolumeMode.SAME: "Источник и назначение на одном томе: перемещение внутри тома.",
        VolumeMode.CROSS: (
            "Источник и назначение на разных томах. Копирование между томами отключено:"
            " план не будет выполнен."
        ),
        VolumeMode.AMBIGUOUS: "Тома не удалось однозначно сопоставить: план отклонён.",
        VolumeMode.UNVERIFIED: "Том не подтверждён: план отклонён.",
    },
    {
        VolumeMode.SAME: "The source and the destination are on one volume: a move within it.",
        VolumeMode.CROSS: (
            "The source and the destination are on different volumes. Copying between"
            " volumes is disabled: the plan will not be run."
        ),
        VolumeMode.AMBIGUOUS: "The volumes could not be matched unambiguously: plan refused.",
        VolumeMode.UNVERIFIED: "The volume is not verified: plan refused.",
    },
)

# the fields ``executor`` names in the detail of SOURCE_CHANGED ("changed since the preview: ...")
CHANGED_FIELD_TEXT = _table(
    {"size": "размер", "mtime": "время изменения", "attributes": "атрибуты"},
    {"size": "size", "mtime": "modification time", "attributes": "attributes"},
)

OPERATION_TEXT = _table(
    {"move": "перемещение", "copy": "копирование", "mkdir": "создание папки"},
    {"move": "move", "copy": "copy", "mkdir": "create folder"},
)

CODE_TABLES: tuple[tuple[type[StrEnum], Mapping[Lang, Mapping]], ...] = (
    (PlanErrorReason, PLAN_ERROR_TEXT),
    (SkipReason, SKIP_TEXT),
    (PlanAction, ACTION_TEXT),
    (ItemStatus, ITEM_STATUS_TEXT),
    (ExecReason, EXEC_TEXT),
    (RunRefusalReason, RUN_REFUSAL_TEXT),
    (OperationState, STATE_TEXT),
    (ReviewFlag, FLAG_TEXT),
    (ExtensionErrorReason, EXTENSION_ERROR_TEXT),
    (VolumeMode, VOLUME_MODE_TEXT),
)
"""Every enum whose codes the user may see, with its texts (checked by the tests)."""


# --- sentences ---------------------------------------------------------------------------

MESSAGES: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        # common
        "no_rollback": (
            "Уже выполненные перемещения не откатываются автоматически.",
            "Moves already made are not rolled back automatically.",
        ),
        "detail": (" ({detail})", " ({detail})"),
        "changed_fields": ("изменилось: {fields}", "changed: {fields}"),
        # destination
        "dest.no_source": ("Сначала выберите исходную папку", "Choose the source folder first"),
        "dest.category": ("<категория>", "<category>"),
        # volume
        "volume.unchecked": (
            "Том не проверялся: выполнение запрещено.",
            "The volume was not checked: execution is not allowed.",
        ),
        "volume.source": ("Источник", "Source"),
        "volume.destination": ("Назначение", "Destination"),
        "volume.missing": (" (нет; ближайший: {deepest})", " (missing; nearest: {deepest})"),
        "volume.location": (
            "{label}: {where}{state} — {fs}, {device}, серийный номер {serial}",
            "{label}: {where}{state} — {fs}, {device}, serial number {serial}",
        ),
        "volume.problem": ("Отказ ({reason}) {path}{detail}", "Refused ({reason}) {path}{detail}"),
        # preview
        "preview.no_source": ("Выберите исходную папку", "Choose the source folder"),
        "preview.failed": ("Предпросмотр не удался: {error}", "The preview failed: {error}"),
        "preview.release": (
            "Не удалось освободить дескриптор при проверке тома: выполнение запрещено",
            "A handle could not be released while checking the volume: execution is not allowed",
        ),
        "preview.headline.errors": (
            "Предпросмотр: выполнение невозможно",
            "Preview: the plan cannot be run",
        ),
        "preview.headline.nothing": (
            "Предпросмотр: перемещать нечего",
            "Preview: nothing to move",
        ),
        "preview.headline.ready": (
            "Предпросмотр готов",
            "Preview ready",
        ),
        "preview.counts": (
            "В плане: {total}. Будет перемещено: {moves} (с новым именем: {renames})."
            " Пропущено: {skipped}.",
            "In the plan: {total}. To be moved: {moves} (with a new name: {renames})."
            " Skipped: {skipped}.",
        ),
        "preview.counts_moves": (
            "Будет перемещено: {moves} (с новым именем: {renames}). Пропущено: {skipped}.",
            "To be moved: {moves} (with a new name: {renames}). Skipped: {skipped}.",
        ),
        "preview.cannot_run": ("Выполнение невозможно:", "The plan cannot be run:"),
        # inventory: every file of the listing the preview was made from (R4-D)
        "inventory.counts": (
            "Файлов в папке: {files}. В плане: {in_plan}. Вне плана: {outside}.",
            "Files in the folder: {files}. In the plan: {in_plan}. Outside the plan: {outside}.",
        ),
        "inventory.files_only": ("Файлов в папке: {files}.", "Files in the folder: {files}."),
        "inventory.unknown": (
            "Сколько файлов в папке — неизвестно: папка не была прочитана.",
            "How many files the folder holds is unknown: the folder was not read.",
        ),
        "inventory.no_rule": ("Нет правила: {groups}.", "No rule: {groups}."),
        "inventory.not_selected": ("Тип не выбран: {groups}.", "Type not chosen: {groups}."),
        "inventory.group": ("{ext} — {count}", "{ext} — {count}"),
        "inventory.type_off": ("тип не выбран", "type not chosen"),
        "inventory.no_extension": ("без расширения", "no extension"),
        "inventory.more": (
            "ещё расширений: {groups} (файлов: {count})",
            "{groups} more extensions ({count} files)",
        ),
        "inventory.hint": (
            "Чтобы включить их в план, выберите тип или добавьте расширение на вкладке"
            " «Типы файлов», затем сделайте новый предпросмотр.",
            "To include them, choose the type or add the extension on the «File types» tab,"
            " then make a new preview.",
        ),
        "inventory.scope": (
            "Считаются файлы только в самой папке, без вложенных папок; по ссылкам программа"
            " не переходит. Числа — на момент предпросмотра.",
            "Only the files in the folder itself are counted, not in subfolders; links are not"
            " followed. The numbers are as of the preview.",
        ),
        "inventory.folders": (
            "Вложенных папок: {count} — они не открываются и не перемещаются.",
            "Subfolders: {count} — they are not opened or moved.",
        ),
        "inventory.links": (
            "Из файлов ссылок: {count} — программа по ним не переходит.",
            "Links among the files: {count} — they are not followed.",
        ),
        "inventory.outside_list": ("Вне плана по расширениям:", "Outside the plan by extension:"),
        # run
        "run.refused": ("Выполнение отклонено: {reason}", "Run refused: {reason}"),
        "run.refused.reason": ("Причина: {reason}.", "Reason: {reason}."),
        "run.refused.unchanged": (
            "Файлы не изменялись, журнал не создавался.",
            "No file was changed, no journal was created.",
        ),
        "run.details": ("Подробности: {detail}", "Details: {detail}"),
        "run.counts": (
            "Перемещено: {moved}. Пропущено: {skipped}. Ошибки: {failed}.",
            "Moved: {moved}. Skipped: {skipped}. Errors: {failed}.",
        ),
        "run.counts.cancelled": ("Отменено: {count}.", "Cancelled: {count}."),
        "run.counts.recovery": ("Требуют проверки: {count}.", "Need a check: {count}."),
        "run.counts.outside": (
            "Вне плана (при предпросмотре): {count}.",
            "Outside the plan (at the preview): {count}.",
        ),
        "run.source_changed": (
            "Изменились после предпросмотра: {count}. Эти файлы не перемещены и остались на"
            " месте. Нажмите «Обновить предпросмотр оставшихся», проверьте новый план и"
            " подтвердите перемещение заново.",
            "Changed after the preview: {count}. These files were not moved and stayed where"
            " they were. Press «Refresh the preview of the remaining files», check the new plan"
            " and confirm the move again.",
        ),
        "run.dirs_created": ("Создано папок: {count}.", "Folders created: {count}."),
        "run.cancelled": (
            "Отменено пользователем: перемещено {moved}",
            "Cancelled by the user: {moved} moved",
        ),
        "run.cancelled.note": (
            "Файл, который перемещался в момент отмены, был завершён.",
            "The file that was being moved when you cancelled was finished.",
        ),
        "run.done": ("Готово: перемещено файлов — {moved}", "Done: files moved — {moved}"),
        "run.nothing": ("Ничего не перемещено", "Nothing was moved"),
        "run.partial": ("Выполнено частично: перемещено {moved}", "Partly done: {moved} moved"),
        "run.stopped": ("Остановлено: {reason}", "Stopped: {reason}"),
        "run.recovery": (
            "Файлов, требующих ручной проверки: {count}. Проверьте их по журналу;"
            " автоматическое восстановление не выполняется.",
            "Files that need a manual check: {count}. Check them in the journal;"
            " nothing is recovered automatically.",
        ),
        "run.skipped_note": (
            "Пропущенные файлы остались на месте. Для них можно сделать новый предпросмотр.",
            "Skipped files stayed where they were. You can make a new preview for them.",
        ),
        "run.problem": ("Проблема: {text}", "Problem: {text}"),
        "run.not_released": ("Не освобождено: {text}", "Not released: {text}"),
        "run.unexpected": ("Неожиданный результат выполнения", "Unexpected result of the run"),
        "run.unexpected.note": (
            "Это не результат перемещения. Проверьте журнал вручную.",
            "This is not the result of a move. Check the journal by hand.",
        ),
        "run.forbidden": ("Выполнение запрещено", "Execution is not allowed"),
        "run.forbidden.note": (
            "Разрешено только перемещение в пределах одного тома. Файлы не изменялись.",
            "Only a move within one volume is allowed. No file was changed.",
        ),
        "run.crashed": ("Выполнение прервано ошибкой", "The run was interrupted by an error"),
        "run.check_journal": (
            "Проверьте последний журнал на вкладке «Журналы».",
            "Check the latest journal on the «Journals» tab.",
        ),
        # journals
        "journal.kind.move": ("Перемещение", "Move"),
        "journal.kind.copy": ("Копирование между томами", "Copy between volumes"),
        "journal.kind.unknown_format": ("Неизвестный формат", "Unknown format"),
        "journal.kind.unknown": ("Неизвестно", "Unknown"),
        "journal.result.finished": ("Завершён", "Finished"),
        "journal.result.failed": ("Завершён с ошибками", "Finished with errors"),
        "journal.result.review": ("Требует ручной проверки", "Needs a manual check"),
        "journal.result.unfinished": (
            "Не завершён: запуск мог прерваться",
            "Not finished: the run may have been interrupted",
        ),
        "journal.result.damaged": (
            "Журнал повреждён или неполон: ничего не подтверждает",
            "The journal is damaged or incomplete: it confirms nothing",
        ),
        "journal.issue.unreadable": (
            "Не удалось прочитать: {detail}",
            "Could not be read: {detail}",
        ),
        "journal.issue.unavailable": ("Недоступен: {detail}", "Not available: {detail}"),
        "journal.issue.not_a_file": (
            "Ссылка или не файл: не читается",
            "A link or not a file: not read",
        ),
        "journal.note.no_localappdata": (
            "LOCALAPPDATA не задан: папка журналов неизвестна",
            "LOCALAPPDATA is not set: the journal folder is unknown",
        ),
        "journal.note.not_created": (
            "Журналов пока нет: папка ещё не создана",
            "No journals yet: the folder is not created",
        ),
        "journal.note.unavailable": (
            "Папка журналов недоступна: {detail}",
            "The journal folder is not available: {detail}",
        ),
        "journal.note.not_a_directory": (
            "Папка журналов — ссылка или не папка: не читается",
            "The journal folder is a link or not a folder: not read",
        ),
        "journal.note.unreadable": (
            "Не удалось прочитать папку: {detail}",
            "The folder could not be read: {detail}",
        ),
        "journal.path": ("Журнал: {path}", "Journal: {path}"),
        "journal.started": ("Начат: {time}", "Started: {time}"),
        "journal.kind": ("Тип: {kind}", "Type: {kind}"),
        "journal.schema": (" (схема v{schema})", " (schema v{schema})"),
        "journal.result": ("Итог: {result}", "Result: {result}"),
        "journal.test": (
            "Тестовый журнал (по эвристике): все пути указывают на временные каталоги pytest.",
            "Test journal (by a heuristic): all paths point to pytest temporary folders.",
        ),
        "journal.read_only": (
            "Только просмотр: восстановление и очистка не выполняются.",
            "View only: nothing is recovered or cleaned up.",
        ),
        "journal.problem": ("Проблема журнала: {text}", "Journal problem: {text}"),
        "journal.entry": (
            "{mark}{index}. {operation}: {state}",
            "{mark}{index}. {operation}: {state}",
        ),
        "journal.entry.from": ("   из: {path}", "   from: {path}"),
        "journal.entry.to": ("   в:  {path}", "   to:   {path}"),
        "journal.entry.phase": ("   этап: {phase}", "   phase: {phase}"),
        "journal.entry.check": ("   проверить: {flags}", "   check: {flags}"),
        "journal.entry.note": ("   примечание: {text}", "   note: {text}"),
        "journal.entry.problem": ("   проблема: {text}", "   problem: {text}"),
        "journal.entry.missing": (
            "   сейчас нет на диске: {path}",
            "   not on the disk now: {path}",
        ),
        "journal.entry.present": ("   сейчас есть на диске: {path}", "   on the disk now: {path}"),
        # settings
        "settings.no_localappdata": (
            "LOCALAPPDATA не задан: настройки не сохраняются",
            "LOCALAPPDATA is not set: settings are not saved",
        ),
        "settings.link": (
            "Файл или папка настроек — ссылка или не обычный файл: не используется",
            "The settings file or folder is a link or not a regular file: not used",
        ),
        "settings.too_large": (
            "Файл настроек слишком большой: не читается",
            "The settings file is too large: not read",
        ),
        "settings.unreadable": (
            "Файл настроек не читается",
            "The settings file cannot be read",
        ),
        "settings.corrupt": (
            "Файл настроек повреждён",
            "The settings file is damaged",
        ),
        "settings.foreign": (
            "Файл настроек не от DownloadsOrganizer или другой версии",
            "The settings file is not from DownloadsOrganizer or is of another version",
        ),
        "settings.unknown_language": (
            "В настройках неизвестный язык",
            "The settings name an unknown language",
        ),
        "settings.rejected": (
            "Пропущены некорректные расширения из настроек: {count}",
            "Invalid extensions in the settings were ignored: {count}",
        ),
        "settings.defaults": (
            "{problem}. Используются стандартные настройки; файл не изменён"
            " (при сохранении его копия будет сохранена рядом).",
            "{problem}. Default settings are used; the file is not changed"
            " (a copy of it is kept when you save).",
        ),
        "settings.partly": (
            "{problem}. Остальные настройки загружены; файл не изменён.",
            "{problem}. The other settings are loaded; the file is not changed.",
        ),
        "settings.saved": ("Настройки сохранены: {path}", "Settings saved: {path}"),
        "settings.saved_backup": (
            "Настройки сохранены: {path}. Прежний файл оставлен для проверки: {backup}",
            "Settings saved: {path}. The previous file was kept for checking: {backup}",
        ),
        "settings.saved_notes": (
            "Настройки сохранены: {path}. После сохранения: {detail}",
            "Settings saved: {path}. After saving: {detail}",
        ),
        "settings.save_failed": (
            "Настройки НЕ сохранены: {reason}. {state} Изменения действуют только в этом окне.",
            "The settings were NOT saved: {reason}. {state} The changes apply only in this window.",
        ),
        "settings.state.intact": ("Файл настроек не изменён.", "The settings file is unchanged."),
        "settings.state.broken": (
            "Файла settings.json сейчас может не быть или он не проверен.",
            "settings.json may be missing now or is not verified.",
        ),
        "settings.state.backup": ("Прежняя версия: {path}.", "The previous version: {path}."),
        "settings.state.temp": (
            "Временный файл этой попытки: {path}.",
            "The temporary file of this attempt: {path}.",
        ),
        "settings.fail.no_localappdata": ("LOCALAPPDATA не задан", "LOCALAPPDATA is not set"),
        "settings.fail.not_read": (
            "файл настроек не был прочитан при загрузке",
            "the settings file was not read when it was loaded",
        ),
        "settings.fail.prepare_failed": (
            "новые настройки не прошли проверку",
            "the new settings did not pass the check",
        ),
        "settings.fail.folder_failed": (
            "папка настроек недоступна",
            "the settings folder is not available",
        ),
        "settings.fail.link": (
            "папка или файл настроек — ссылка, junction или не обычный объект",
            "the settings folder or file is a link, a junction or not a regular object",
        ),
        "settings.fail.unverified": (
            "расположение папки настроек не подтверждено",
            "the location of the settings folder is not proven",
        ),
        "settings.fail.busy": (
            "настройки сейчас сохраняет другое окно программы",
            "another window of the program is saving the settings now",
        ),
        "settings.fail.conflict": (
            "файл настроек изменён после загрузки (другой программой или окном)",
            "the settings file changed after it was loaded (by another program or window)",
        ),
        "settings.fail.read_failed": (
            "текущий файл настроек не читается",
            "the current settings file cannot be read",
        ),
        "settings.fail.temp_failed": (
            "не удалось создать временный файл",
            "the temporary file could not be created",
        ),
        "settings.fail.write_failed": (
            "не удалось записать временный файл",
            "the temporary file could not be written",
        ),
        "settings.fail.backup_failed": (
            "не удалось отложить прежний файл",
            "the previous file could not be set aside",
        ),
        "settings.fail.replace_failed": (
            "не удалось поставить новый файл на место",
            "the new file could not be put in place",
        ),
        "settings.fail.verify_failed": (
            "новый файл не прошёл проверку после замены",
            "the new file did not pass the check after the replacement",
        ),
        "settings.reloaded": (
            "Настройки загружены из файла заново.",
            "The settings were loaded from the file again.",
        ),
        "settings.reload_busy": (
            "Загрузить настройки заново можно после предпросмотра или перемещения.",
            "The settings can be loaded again after the preview or the move.",
        ),
        # window
        "gui.area.sort": ("Сортировка", "Sorting"),
        "gui.area.types": ("Типы файлов", "File types"),
        "gui.area.journals": ("Журналы", "Journals"),
        "gui.source": ("Откуда", "From"),
        "gui.destination": ("Куда", "To"),
        "gui.browse": ("Обзор…", "Browse…"),
        "gui.browse.title": ("Выберите папку", "Choose a folder"),
        "gui.reset": ("Сбросить", "Reset"),
        "gui.effective": (
            "Файлы будут разложены в: {path}",
            "Files will be sorted into: {path}",
        ),
        "gui.policy": ("Если имя занято:", "If the name is taken:"),
        "gui.policy.rename": ("Новое имя", "New name"),
        "gui.policy.skip": ("Пропустить", "Skip"),
        "gui.to_types": ("Типы файлов…", "File types…"),
        "gui.selected": (
            "Выбрано типов: {selected} из {total}",
            "Types selected: {selected} of {total}",
        ),
        "gui.select_all": ("Выбрать все", "Select all"),
        "gui.select_none": ("Снять все", "Clear all"),
        "gui.preview": ("Предпросмотр", "Preview"),
        "gui.move": ("Переместить…", "Move…"),
        "gui.cancel": ("Отмена", "Cancel"),
        "gui.repreview": (
            "Обновить предпросмотр оставшихся",
            "Refresh the preview of the remaining files",
        ),
        "gui.copy_path": ("Копировать путь", "Copy path"),
        "gui.details.show": ("Подробности ▸", "Details ▸"),
        "gui.details.hide": ("Подробности ▾", "Details ▾"),
        "gui.details.title": (
            "Подробности: пути и проверка тома",
            "Details: paths and the volume check",
        ),
        "gui.types.search": ("Найти расширение: .pdf", "Find an extension: .pdf"),
        "gui.types.found": ("Найдено категорий: {count}", "Categories found: {count}"),
        "gui.types.not_found": (
            "Расширения «{text}» нет в таблице — его можно добавить как своё правило.",
            "«{text}» is not in the table — it can be added as your own rule.",
        ),
        "gui.copied": ("Путь скопирован", "Path copied"),
        "gui.copy_failed": (
            "Не удалось скопировать путь: буфер обмена недоступен ({reason})",
            "The path could not be copied: the clipboard is not available ({reason})",
        ),
        "gui.col.file": ("Файл", "File"),
        "gui.col.category": ("Категория", "Category"),
        "gui.col.action": ("Действие", "Action"),
        "gui.col.reason": ("Причина", "Reason"),
        "gui.col.target": ("Куда", "Destination"),
        "gui.col.status": ("Итог", "Status"),
        "gui.col.time": ("Время", "Time"),
        "gui.col.operation": ("Операция", "Operation"),
        "gui.col.result": ("Результат", "Result"),
        "gui.col.files": ("Файлов", "Files"),
        "gui.filter.all": ("Все", "All"),
        "gui.filter.not_moved": ("Не перемещённые", "Not moved"),
        "gui.detail.file": ("Файл: {value}", "File: {value}"),
        "gui.detail.source": ("Откуда: {value}", "From: {value}"),
        "gui.detail.target": ("Куда: {value}", "To: {value}"),
        "gui.detail.category": ("Категория: {value}", "Category: {value}"),
        "gui.detail.action": ("Действие: {value}", "Action: {value}"),
        "gui.detail.reason": ("Причина: {value}", "Reason: {value}"),
        "gui.detail.status": ("Итог: {value}", "Status: {value}"),
        "gui.preview.none": ("Предпросмотра ещё нет", "No preview yet"),
        "gui.preview.how": (
            "Выберите папку и типы файлов, затем нажмите «Предпросмотр».",
            "Choose the folder and the file types, then press «Preview».",
        ),
        "gui.preview.safe": (
            "Предпросмотр ничего не меняет: он только показывает, что будет перемещено."
            " Перемещение начнётся только после «Переместить…» и подтверждения.",
            "The preview changes nothing: it only shows what would be moved."
            " Moving starts only after «Move…» and a confirmation.",
        ),
        "gui.preview.stale": (
            "Настройки изменились: этот предпросмотр устарел, сделайте новый",
            "The settings changed: this preview is out of date, make a new one",
        ),
        "gui.preview.select_row": (
            "Выберите строку, чтобы увидеть полный путь файла.",
            "Choose a row to see the full path of the file.",
        ),
        "gui.preview.updating": ("Строится предпросмотр…", "Making the preview…"),
        "gui.preview.used": ("Выполняется перемещение по этому плану…", "Moving by this plan…"),
        "gui.status.previewing": (
            "Строится предпросмотр. Файлы не изменяются.",
            "Making the preview. No file is changed.",
        ),
        "gui.status.ready": (
            "Проверьте план и нажмите «Переместить…».",
            "Check the plan and press «Move…».",
        ),
        "gui.status.fix": (
            "Исправьте ошибки и сделайте новый предпросмотр.",
            "Fix the errors and make a new preview.",
        ),
        "gui.status.changed": (
            "Настройки изменились. Перед перемещением нужен новый предпросмотр.",
            "The settings changed. A new preview is needed before moving.",
        ),
        "gui.status.changed_during_preview": (
            "Настройки изменились во время предпросмотра: он отброшен. Сделайте новый.",
            "The settings changed during the preview: it was dropped. Make a new one.",
        ),
        "gui.status.need_preview": (
            "Сначала сделайте предпросмотр без ошибок.",
            "Make an error-free preview first.",
        ),
        "gui.status.stale_after_dialog": (
            "План изменился, пока было открыто подтверждение: ничего не перемещено."
            " Сделайте новый предпросмотр.",
            "The plan changed while the confirmation was open: nothing was moved."
            " Make a new preview.",
        ),
        "gui.status.declined": (
            "Перемещение не начато. Файлы не изменялись.",
            "Moving was not started. No file was changed.",
        ),
        "gui.status.running": (
            "Идёт перемещение. «Отмена» остановит его после текущего файла.",
            "Moving. «Cancel» stops it after the current file.",
        ),
        "gui.status.cancel_requested": (
            "Отмена запрошена: перемещение остановится после текущего файла.",
            "Cancel requested: moving stops after the current file.",
        ),
        "gui.status.closing": (
            "Окно закроется, когда перемещение остановится после текущего файла.",
            "The window closes once moving stops after the current file.",
        ),
        "gui.confirm.title": ("Подтверждение перемещения", "Confirm the move"),
        "gui.confirm.heading": ("Переместить файлы: {count}?", "Move {count} files?"),
        "gui.confirm.counts": (
            "Из них с новым именем: {renames}. Пропущено и останется на месте: {skipped}.",
            "With a new name: {renames}. Skipped and left in place: {skipped}.",
        ),
        "gui.confirm.from": ("Откуда: {path}", "From: {path}"),
        "gui.confirm.to": ("Куда: {path}", "To: {path}"),
        "gui.confirm.move": (
            "Файлы будут перемещены, а не скопированы: в исходной папке их не останется."
            " Существующие файлы не перезаписываются.",
            "The files are moved, not copied: they will no longer be in the source folder."
            " Existing files are never overwritten.",
        ),
        "gui.confirm.journal": (
            "Каждое действие записывается в журнал в: {path}",
            "Every action is logged in: {path}",
        ),
        "gui.confirm.journal_unknown": (
            "Папка журналов неизвестна (LOCALAPPDATA не задан).",
            "The journal folder is unknown (LOCALAPPDATA is not set).",
        ),
        "gui.confirm.accept": ("Переместить", "Move"),
        "gui.close.title": ("Идёт перемещение", "Moving in progress"),
        "gui.close.heading": ("Перемещение ещё идёт", "Moving is still in progress"),
        "gui.close.body": (
            "Можно отменить его и закрыть окно: текущий файл будет завершён, остальные"
            " останутся на месте. Уже перемещённые файлы не возвращаются.",
            "You can cancel it and close the window: the current file is finished, the rest"
            " stay in place. Files already moved are not moved back.",
        ),
        "gui.close.accept": ("Отменить и закрыть", "Cancel and close"),
        "gui.close.keep": ("Продолжить перемещение", "Keep moving"),
        "gui.settings.save": ("Сохранить настройки", "Save settings"),
        "gui.settings.unsaved": ("● Не сохранено", "● Not saved"),
        "gui.unsaved.title": ("Несохранённые настройки", "Unsaved settings"),
        "gui.unsaved.heading": (
            "Сохранить изменения настроек?",
            "Save the changed settings?",
        ),
        "gui.unsaved.body": (
            "Язык или свои расширения изменены и не сохранены. Если не сохранить, при"
            " следующем запуске будут прежние настройки.",
            "The language or the own extensions changed and are not saved. If you do not save"
            " them, the next start uses the previous settings.",
        ),
        "gui.unsaved.save": ("Сохранить", "Save"),
        "gui.unsaved.discard": ("Не сохранять", "Don't save"),
        "gui.reload.title": ("Файл настроек изменён", "The settings file changed"),
        "gui.reload.heading": (
            "Загрузить настройки из файла?",
            "Load the settings from the file?",
        ),
        "gui.reload.body": (
            "Файл настроек изменён после загрузки, и ваши изменения не записаны поверх него."
            " Можно загрузить настройки из файла: несохранённые изменения этого окна будут"
            " потеряны.",
            "The settings file changed after it was loaded, and your changes were not written"
            " over it. You can load the settings from the file: the unsaved changes of this"
            " window are lost.",
        ),
        "gui.reload.accept": ("Загрузить из файла", "Load from the file"),
        "gui.reload.keep": ("Оставить мои изменения", "Keep my changes"),
        "gui.card.count": ("{selected} из {total}", "{selected} of {total}"),
        "gui.card.more": ("▸ Ещё {count}", "▸ {count} more"),
        "gui.card.more_selected": (
            "▸ Ещё {count} (выбрано {selected})",
            "▸ {count} more ({selected} selected)",
        ),
        "gui.card.less": ("▾ Скрыть {count}", "▾ Hide {count}"),
        "gui.rules.add_label": ("Своё расширение:", "Own extension:"),
        "gui.rules.placeholder": ("напр. .avif", "e.g. .avif"),
        "gui.rules.add": ("Добавить", "Add"),
        "gui.rules.hint": (
            "Добавьте расширение в существующую категорию. Стандартные расширения не меняются."
            " Изменения записываются кнопкой «Сохранить настройки».",
            "Add an extension to an existing category. The standard extensions do not change."
            " Changes are written by «Save settings».",
        ),
        "gui.rules.invalid": ("Не добавлено «{value}»: {reason}", "«{value}» not added: {reason}"),
        "gui.rules.added": (
            "{extension} добавлено в «{category}». Отметьте его, чтобы включить в сортировку.",
            "{extension} added to «{category}». Tick it to include it in sorting.",
        ),
        "gui.rules.deleted": (
            "{extension} удалено из своих расширений.",
            "{extension} removed from own extensions.",
        ),
        "gui.rules.restored": (
            "Восстановлен стандартный набор: свои расширения удалены.",
            "The standard set is restored: own extensions were removed.",
        ),
        "gui.rules.title": ("Свои расширения", "Own extensions"),
        "gui.rules.empty": ("Своих расширений нет.", "No own extensions."),
        "gui.rules.rule": ("{extension} → {category}", "{extension} → {category}"),
        "gui.rules.enabled": ("Включено", "Enabled"),
        "gui.rules.delete": ("Удалить", "Delete"),
        "gui.rules.restore": ("Восстановить стандартный набор", "Restore the standard set"),
        "gui.rules.legend": ("* — своё расширение", "* — own extension"),
        "gui.restore.title": ("Стандартный набор", "Standard set"),
        "gui.restore.heading": (
            "Удалить свои расширения: {count}?",
            "Remove {count} own extensions?",
        ),
        "gui.restore.body": (
            "Останутся только стандартные расширения. Файлы не изменяются.",
            "Only the standard extensions remain. No file is changed.",
        ),
        "gui.restore.accept": ("Удалить", "Remove"),
        "gui.journals.refresh": ("Обновить", "Refresh"),
        "gui.journals.loading": ("Чтение списка журналов…", "Reading the journal list…"),
        "gui.journals.failed": (
            "Не удалось прочитать журналы: {error}",
            "The journals could not be read: {error}",
        ),
        "gui.journals.summary": ("{path} — журналов: {count}.", "{path} — journals: {count}."),
        "gui.journals.unknown": ("Папка журналов неизвестна", "The journal folder is unknown"),
        "gui.journals.tests": ("Тестовых: {count}.", "Test journals: {count}."),
        "gui.journals.hint": (
            "Выберите журнал, чтобы увидеть подробности. Журналы только просматриваются:"
            " ничего не восстанавливается и не удаляется.",
            "Choose a journal to see its details. Journals are only viewed:"
            " nothing is recovered or deleted.",
        ),
        "gui.journals.files.done": ("{done} из {total}", "{done} of {total}"),
        "gui.journals.check.test": ("тестовый", "test"),
        "gui.journals.check.recovery": ("проверить вручную: {count}", "check by hand: {count}"),
        "gui.journals.check.missing": ("файлов нет на месте", "files are not in place"),
        "gui.internal_error": (
            "Внутренняя ошибка окна: {error}\nФайлы не перемещались этим действием.",
            "Internal error of the window: {error}\nThis action moved no file.",
        ),
    }
)


def tr(key: str, lang: Lang, **values: object) -> str:
    """The sentence ``key`` in ``lang`` with ``values`` filled in; an unknown key as is."""
    texts = MESSAGES.get(key)
    if texts is None:
        return key
    text = texts[0] if Lang(lang) is Lang.RU else texts[1]
    return text.format(**values) if values else text


def placeholders(text: str) -> frozenset[str]:
    """The ``{names}`` of a format string (for the tests)."""
    return frozenset(name for _, name, _, _ in string.Formatter().parse(text) if name)
