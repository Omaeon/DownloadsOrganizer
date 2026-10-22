"""``appmodel`` (R4-A, R4-C): what the window shows and allows, without any widget.

Every folder is below ``tmp_path``; ``LOCALAPPDATA`` is the isolated one of conftest, so
the journals of the runs here never reach the user's profile. A destination on another
volume is faked (``fake_destination``), never a real second drive.
"""

from __future__ import annotations

import hashlib
import queue
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from test_executor import TABLE, touch
from test_volumeproof import EXTS, V999, fake_destination

from downloads_organizer import appmodel, features
from downloads_organizer.appmodel import (
    ACTION_TEXT,
    CATEGORY_NAMES,
    EXEC_TEXT,
    FLAG_TEXT,
    ITEM_STATUS_TEXT,
    PLAN_ERROR_TEXT,
    RUN_REFUSAL_TEXT,
    SKIP_TEXT,
    STATE_TEXT,
    AppModel,
    BackgroundTask,
    JournalKind,
    JournalResult,
    Lang,
    Level,
    PreviewResult,
    Settings,
    TaskDone,
    TaskKind,
    build_preview,
    effective_destination,
    execute,
    journal_details,
    list_journals,
    run_report,
    summarize_journal,
    summarize_outcome,
)
from downloads_organizer.catalog import DEFAULT_CATEGORIES
from downloads_organizer.executor import ExecReason, ItemStatus
from downloads_organizer.journal import (
    Journal,
    Operation,
    OperationState,
    Status,
    default_log_dir,
)
from downloads_organizer.journal_v2 import ReviewFlag
from downloads_organizer.models import (
    CollisionPolicy,
    PlanAction,
    PlanErrorReason,
    SkipReason,
    VolumeMode,
)
from downloads_organizer.runner import RunRefusal, RunRefusalReason
from downloads_organizer.volumeproof import preview_plan

pytestmark = pytest.mark.windows_only

Snapshot = Callable[[Path], dict[str, tuple[object, ...]]]
CROSS_TEXT = "Копирование между томами пока отключено"


@pytest.fixture
def src(tmp_path: Path) -> Path:
    path = tmp_path / "area" / "Downloads"
    touch(path, "a.pdf", "b.jpg", "c.mp3", "notes.txt")
    return path


def settings(
    source: Path | str,
    destination: Path | str | None = None,
    selected: frozenset[str] = EXTS,
    policy: CollisionPolicy = CollisionPolicy.RENAME,
) -> Settings:
    return Settings(
        str(source), None if destination is None else str(destination), selected, policy
    )


def model_for(src: Path, selected: frozenset[str] = EXTS) -> AppModel:
    model = AppModel(TABLE)
    model.set_source(str(src))
    for ext in selected:
        model.set_extension(ext, True)
    return model


def previewed(model: AppModel) -> PreviewResult:
    result = build_preview(model.settings(), model.table)
    assert model.accept_preview(result)
    return result


def no_cancel() -> bool:
    return False


# --- texts -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("enum", "texts"),
    [
        (PlanErrorReason, PLAN_ERROR_TEXT),
        (SkipReason, SKIP_TEXT),
        (PlanAction, ACTION_TEXT),
        (ItemStatus, ITEM_STATUS_TEXT),
        (ExecReason, EXEC_TEXT),
        (RunRefusalReason, RUN_REFUSAL_TEXT),
        (OperationState, STATE_TEXT),
        (ReviewFlag, FLAG_TEXT),
    ],
)
def test_every_reason_has_a_russian_text(enum: type, texts: dict[object, str]) -> None:
    assert set(texts) == set(enum)
    assert all(text and any("а" <= ch.lower() <= "я" for ch in text) for text in texts.values())


def test_every_category_has_a_russian_name() -> None:
    assert set(CATEGORY_NAMES) == {c.key for c in DEFAULT_CATEGORIES}


def test_cross_volume_text_says_copy_is_disabled() -> None:
    assert PLAN_ERROR_TEXT[PlanErrorReason.CROSS_VOLUME].startswith(CROSS_TEXT)


def test_effective_destination(tmp_path: Path) -> None:
    assert effective_destination("", None) == "Сначала выберите исходную папку"
    assert effective_destination(str(tmp_path), None) == str(tmp_path / "DEL" / "<категория>")
    assert effective_destination(str(tmp_path), "  ") == str(tmp_path / "DEL" / "<категория>")
    out = tmp_path / "Sorted"
    assert effective_destination(str(tmp_path), str(out)) == str(out / "<категория>")


# --- preview ------------------------------------------------------------------------------


def test_preview_changes_nothing(
    src: Path, tmp_path: Path, snapshot: Snapshot, no_side_effects, isolated_user_dirs: Path
) -> None:
    before, home = snapshot(tmp_path / "area"), snapshot(isolated_user_dirs)
    with no_side_effects():
        result = build_preview(settings(src), TABLE)
    assert snapshot(tmp_path / "area") == before and snapshot(isolated_user_dirs) == home
    assert result.executable and result.plan is not None
    assert result.plan.cross_copy is False
    assert result.plan.volume is not None and result.plan.volume.mode is VolumeMode.SAME


def test_preview_rows_show_file_category_target_and_action(src: Path) -> None:
    result = build_preview(settings(src), TABLE)
    rows = {r.name: r for r in result.rows()}
    assert set(rows) == {"a.pdf", "b.jpg", "c.mp3"}  # notes.txt is not selected
    assert rows["b.jpg"].category == "🖼 Изображения (Images)"
    assert rows["a.pdf"].target == str(src / "DEL" / "PDF" / "a.pdf")
    assert rows["a.pdf"].action == "Переместить" and rows["a.pdf"].reason == ""
    assert (result.total, result.moves, result.renames, result.skipped) == (3, 3, 0, 0)
    assert result.errors() == () and result.volume_lines()
    assert result.headline().startswith("Предпросмотр готов")


@pytest.mark.parametrize(
    ("policy", "action", "reason"),
    [
        (CollisionPolicy.RENAME, "Переместить с новым именем", ""),
        (CollisionPolicy.SKIP, "Пропустить", "Файл с таким именем уже есть в назначении"),
    ],
)
def test_preview_shows_collisions(
    src: Path, policy: CollisionPolicy, action: str, reason: str
) -> None:
    touch(src / "DEL" / "PDF", "a.pdf")
    result = build_preview(settings(src, policy=policy), TABLE)
    row = next(r for r in result.rows() if r.name == "a.pdf")
    assert row.action == action and row.reason.startswith(reason)
    assert row.skipped is (policy is CollisionPolicy.SKIP)
    assert row.renamed is (policy is CollisionPolicy.RENAME)


def test_preview_shows_why_a_file_is_skipped(src: Path, set_attributes) -> None:
    import stat as stat_module

    set_attributes(src / "a.pdf", stat_module.FILE_ATTRIBUTE_HIDDEN)
    result = build_preview(settings(src), TABLE)
    row = next(r for r in result.rows() if r.name == "a.pdf")
    assert row.skipped and row.reason.startswith(SKIP_TEXT[SkipReason.HIDDEN])
    assert result.skipped == 1 and result.executable  # the other two still move


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda src: settings(src, selected=frozenset()), "Не выбрано ни одного типа файлов"),
        (lambda src: settings(src / "missing"), "Исходная папка не найдена"),
        (lambda src: settings(src, src), "Папка назначения совпадает с исходной"),
        (lambda src: settings(""), "Выберите исходную папку"),
    ],
)
def test_preview_with_errors_is_not_executable(
    src: Path, make: Callable[[Path], Settings], message: str
) -> None:
    result = build_preview(make(src), TABLE)
    assert not result.executable and result.errors() and result.errors()[0].startswith(message)
    assert result.headline() == "Предпросмотр: выполнение невозможно"


def test_preview_with_nothing_to_move_is_not_executable(src: Path) -> None:
    result = build_preview(settings(src, selected=frozenset({".zip"})), TABLE)
    assert result.errors() == () and result.total == 0 and not result.executable


def test_preview_survives_an_unexpected_error(src: Path) -> None:
    result = build_preview(settings(src, selected=frozenset({".unknown"})), TABLE)
    assert result.plan is None and not result.executable
    assert result.errors()[0].startswith("Предпросмотр не удался")


@pytest.mark.parametrize("flag", [False, True])
def test_other_volume_is_refused_and_never_becomes_a_copy(
    src: Path, monkeypatch: pytest.MonkeyPatch, flag: bool
) -> None:
    monkeypatch.setattr(features, "CROSS_VOLUME_COPY", flag)
    fake_destination(monkeypatch, device=V999, volume_serial=0x1234)
    result = build_preview(settings(src), TABLE)
    assert result.plan is not None and result.plan.cross_copy is False
    assert [e.reason for e in result.plan.errors] == [PlanErrorReason.CROSS_VOLUME]
    [error] = result.errors()
    assert error == f"{PLAN_ERROR_TEXT[PlanErrorReason.CROSS_VOLUME]}: {src / 'DEL'}"
    assert not result.executable and result.moves == 0


# --- the model: a preview belongs to its settings -------------------------------------------


def test_nothing_is_selected_and_nothing_can_run_at_start() -> None:
    model = AppModel()
    assert model.selection.selected_extensions == frozenset()
    assert model.preview is None and not model.can_execute and not model.running
    with pytest.raises(RuntimeError):
        model.start_run()


def test_a_preview_makes_the_model_executable(src: Path) -> None:
    model = model_for(src)
    previewed(model)
    assert model.can_execute


@pytest.mark.parametrize(
    "change",
    [
        lambda m, tmp: m.set_source(str(tmp)),
        lambda m, tmp: m.set_destination(str(tmp / "Sorted")),
        lambda m, tmp: m.set_policy(CollisionPolicy.SKIP),
        lambda m, tmp: m.set_extension(".txt", True),
        lambda m, tmp: m.set_extension(".pdf", False),
        lambda m, tmp: m.set_category("Images", True),
        lambda m, tmp: m.set_all(True),
        lambda m, tmp: m.set_all(False),
    ],
)
def test_every_change_drops_the_preview(
    src: Path, tmp_path: Path, change: Callable[[AppModel, Path], bool]
) -> None:
    model = model_for(src)
    previewed(model)
    assert change(model, tmp_path) is True
    assert model.preview is None and not model.can_execute


def test_setting_the_same_value_keeps_the_preview(src: Path) -> None:
    model = model_for(src)
    previewed(model)
    assert model.set_source(str(src)) is False
    assert model.set_destination("   ") is False  # blank is no destination, as before
    assert model.set_extension(".pdf", True) is False
    assert model.can_execute


def test_a_preview_for_other_settings_is_not_accepted(src: Path, tmp_path: Path) -> None:
    model = model_for(src)
    result = build_preview(model.settings(), model.table)
    model.set_destination(str(tmp_path / "Sorted"))  # changed while the preview ran
    assert not model.accept_preview(result)
    assert model.preview is None and not model.can_execute


def test_a_run_uses_the_preview_up_and_blocks_a_second_run(src: Path) -> None:
    model = model_for(src)
    result = previewed(model)
    plan = model.start_run()
    assert plan is result.plan and model.running and model.preview is None
    assert not model.can_execute and not model.accept_preview(result)
    with pytest.raises(RuntimeError):
        model.start_run()
    model.finish_run()
    assert not model.running and not model.can_execute  # a new preview is needed


# --- execute --------------------------------------------------------------------------------


def test_execute_moves_on_one_volume_and_writes_a_journal(
    src: Path, isolated_user_dirs: Path
) -> None:
    model = model_for(src)
    previewed(model)
    report = run_report(execute(model.start_run(), model.table, no_cancel))
    assert report.level is Level.OK and report.headline == "Готово: перемещено файлов — 3"
    assert report.counts[ItemStatus.MOVED] == 3 and report.changed_anything
    assert (src / "DEL" / "PDF" / "a.pdf").is_file() and not (src / "a.pdf").exists()
    assert (src / "notes.txt").is_file()
    assert report.journal_path is not None
    assert report.journal_path.parent == default_log_dir()
    assert report.journal_path.is_relative_to(isolated_user_dirs)
    assert {r.status for r in report.rows} == {"Перемещено"}
    listing = list_journals()
    assert [j.path for j in listing.journals] == [report.journal_path]
    journal = listing.journals[0]
    assert (journal.kind, journal.result, journal.recovery) == (
        JournalKind.MOVE,
        JournalResult.FINISHED,
        0,
    )
    assert journal.states == {OperationState.DONE: 3} and journal.test_journal


def test_cancel_before_the_first_file_moves_nothing(src: Path, snapshot: Snapshot) -> None:
    model = model_for(src)
    previewed(model)
    before = snapshot(src)
    report = run_report(execute(model.start_run(), model.table, lambda: True))
    assert report.counts[ItemStatus.CANCELLED] == 3 and report.counts[ItemStatus.MOVED] == 0
    assert report.level is Level.WARNING and report.headline.startswith("Отменено")
    assert any("не откатываются" in line for line in report.lines)
    assert all(p in before for p in snapshot(src))  # only the journal is new, elsewhere
    assert (src / "a.pdf").is_file()


def test_cancel_after_the_first_file_stops_the_rest(src: Path) -> None:
    model = model_for(src)
    previewed(model)
    asked: list[int] = []

    def cancel() -> bool:
        asked.append(1)
        return len(asked) > 1

    report = run_report(execute(model.start_run(), model.table, cancel))
    assert report.counts[ItemStatus.MOVED] == 1 and report.counts[ItemStatus.CANCELLED] == 2
    assert report.headline == "Отменено пользователем: перемещено 1"


def test_execute_refuses_a_copy_plan_before_run_plan(
    src: Path, monkeypatch: pytest.MonkeyPatch, isolated_user_dirs: Path, snapshot: Snapshot
) -> None:
    monkeypatch.setattr(features, "CROSS_VOLUME_COPY", True)
    fake_destination(monkeypatch, device=V999, volume_serial=0x1234)
    plan = preview_plan(src, None, TABLE, EXTS, cross_copy=True).plan
    assert plan.ok and plan.cross_copy and plan.volume is not None
    monkeypatch.setattr(appmodel, "run_plan", lambda *a, **k: pytest.fail("run_plan called"))
    before, home = snapshot(src), snapshot(isolated_user_dirs)
    report = run_report(execute(plan, TABLE, no_cancel))
    assert report.level is Level.ERROR and report.headline == "Выполнение запрещено"
    assert not report.changed_anything and report.journal_path is None
    assert snapshot(src) == before and snapshot(isolated_user_dirs) == home


def test_execute_refuses_a_plan_with_errors(
    src: Path, isolated_user_dirs: Path, snapshot: Snapshot
) -> None:
    plan = build_preview(settings(src, selected=frozenset()), TABLE).plan
    assert plan is not None and not plan.ok
    home = snapshot(isolated_user_dirs)
    report = run_report(execute(plan, TABLE, no_cancel))
    assert report.level is Level.ERROR and not report.changed_anything
    assert snapshot(isolated_user_dirs) == home


def test_a_stale_plan_is_refused_by_run_plan(src: Path, isolated_user_dirs: Path) -> None:
    model = model_for(src)
    previewed(model)
    plan = model.start_run()
    (src / "DEL").mkdir()  # the destination appeared after the preview
    report = run_report(execute(plan, model.table, no_cancel))
    assert report.level is Level.ERROR and not report.changed_anything
    assert (
        report.headline == "Выполнение отклонено: " + RUN_REFUSAL_TEXT[RunRefusalReason.PLAN_STALE]
    )
    assert "Файлы не изменялись, журнал не создавался." in report.lines
    assert (src / "a.pdf").is_file()
    assert not (isolated_user_dirs / "AppData" / "Local" / "DownloadsOrganizer").exists()


def test_refusal_report_names_the_reason() -> None:
    report = summarize_outcome(RunRefusal(None, RunRefusalReason.COPY_DISABLED, "flag off"))
    assert report.headline == "Выполнение отклонено: Копирование между томами пока отключено"
    assert report.lines[-1] == "Подробности: flag off" and not report.changed_anything


def test_an_unknown_outcome_is_not_hidden() -> None:
    report = summarize_outcome(object())
    assert report.level is Level.ERROR and report.changed_anything


def test_execute_reports_an_unexpected_error(src: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model = model_for(src)
    previewed(model)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(appmodel, "run_plan", boom)
    report = run_report(execute(model.start_run(), model.table, no_cancel))
    assert report.level is Level.ERROR and report.changed_anything
    assert "RuntimeError: boom" in report.lines[0]


# --- background tasks ---------------------------------------------------------------------


def test_background_task_reports_its_value_and_sees_cancel() -> None:
    events: queue.Queue[TaskDone] = queue.Queue()
    started, release = threading.Event(), threading.Event()

    def job(cancelled: Callable[[], bool]) -> bool:
        started.set()
        release.wait(5)
        return cancelled()

    task = BackgroundTask(TaskKind.RUN, job, events)
    assert task._thread.daemon is False  # a run is never cut off by the end of the process
    task.start()
    assert started.wait(5)
    task.cancel()
    release.set()
    task.join(5)
    assert events.get(timeout=5) == TaskDone(TaskKind.RUN, True)
    assert not task.alive


def test_background_task_reports_an_error() -> None:
    events: queue.Queue[TaskDone] = queue.Queue()

    def job(_cancelled: Callable[[], bool]) -> None:
        raise ValueError("bad")

    task = BackgroundTask(TaskKind.PREVIEW, job, events)
    assert task._thread.daemon is True
    task.start()
    task.join(5)
    done = events.get(timeout=5)
    assert done.kind is TaskKind.PREVIEW and done.value is None
    assert done.error == "ValueError: bad"


# --- journals ---------------------------------------------------------------------------------


def write_journal(directory: Path, *records: tuple[object, ...], finish: bool = True) -> Path:
    with Journal.create(directory) as log:
        log.write(Operation.RUN, Status.STARTED)
        for operation, status, kwargs in records:
            log.write(operation, status, **kwargs)  # type: ignore[arg-type]
        if finish:
            log.write(Operation.RUN, Status.FINISHED)
        assert log.path is not None
        return log.path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_missing_journal_directory_is_not_created(isolated_user_dirs: Path) -> None:
    listing = list_journals()
    assert listing.journals == () and listing.note is not None
    assert listing.directory == default_log_dir()
    assert not (isolated_user_dirs / "AppData" / "Local" / "DownloadsOrganizer").exists()


def test_unknown_journal_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOCALAPPDATA")
    listing = list_journals()
    assert listing.directory is None and "LOCALAPPDATA" in (listing.note_text() or "")


def test_startup_writes_nothing_to_the_profile(
    isolated_user_dirs: Path, snapshot: Snapshot
) -> None:
    before = snapshot(isolated_user_dirs)
    model = AppModel()
    model.effective_destination()
    list_journals()
    assert snapshot(isolated_user_dirs) == before


def test_a_finished_journal(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    moved = tmp_path / "out" / "a.pdf"
    touch(moved.parent, "a.pdf")
    path = write_journal(
        logs,
        (Operation.MOVE, Status.INTENT, {"source": tmp_path / "a.pdf", "target": moved}),
        (Operation.MOVE, Status.DONE, {"intent": 2, "source": tmp_path / "a.pdf", "target": moved}),
    )
    before = digest(path)
    [journal] = list_journals(logs).journals
    assert digest(path) == before and sorted(p.name for p in logs.iterdir()) == [path.name]
    assert (journal.path, journal.schema, journal.kind) == (path, 1, JournalKind.MOVE)
    assert (
        journal.result is JournalResult.FINISHED and journal.recovery == 0 and not journal.attention
    )
    assert journal.started is not None and journal.started.tzinfo is not None
    assert journal.test_journal  # every path is in a pytest temporary directory
    assert not journal.missing_objects  # only entries that need a check are looked up
    text = journal_details(journal)
    assert str(path) in text and "Тестовый журнал" in text and "только" in text.lower()


def test_a_journal_outside_pytest_is_not_called_a_test(tmp_path: Path) -> None:
    path = write_journal(
        tmp_path / "logs",
        (
            Operation.MOVE,
            Status.INTENT,
            {
                "source": Path(r"C:\Users\x\Downloads\a.pdf"),
                "target": Path(r"C:\Users\x\Downloads\DEL\PDF\a.pdf"),
            },
        ),
        (Operation.MOVE, Status.DONE, {"intent": 2}),
    )
    assert not summarize_journal(path).test_journal


def test_an_unfinished_journal_needs_a_check_and_shows_missing_objects(
    tmp_path: Path, snapshot: Snapshot
) -> None:
    logs, source, target = tmp_path / "logs", tmp_path / "gone.pdf", tmp_path / "T" / "gone.pdf"
    path = write_journal(
        logs, (Operation.MOVE, Status.INTENT, {"source": source, "target": target}), finish=False
    )
    before = snapshot(tmp_path)
    [journal] = list_journals(logs).journals
    assert snapshot(tmp_path) == before  # nothing recovered, repaired or created
    assert journal.result is JournalResult.UNFINISHED
    assert journal.recovery == 1 and journal.attention and journal.missing_objects
    [entry] = [e for e in journal.entries if e.operation == "move"]
    assert entry.needs_review and set(entry.missing) == {str(source), str(target)}
    assert "сейчас нет на диске" in journal_details(journal)
    assert path.is_file()


def test_a_damaged_journal_is_reported_not_repaired(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    path = write_journal(logs)
    with path.open("ab") as stream:
        stream.write(b"{not json\n")
    before = digest(path)
    journal = summarize_journal(path)
    assert digest(path) == before
    assert journal.result is JournalResult.DAMAGED and journal.problems
    assert "Журнал повреждён" in journal_details(journal)
    assert journal.attention


def test_links_and_directories_are_listed_not_read(tmp_path: Path, make_junction) -> None:
    logs = tmp_path / "logs"
    write_journal(logs)
    (logs / "other.txt").write_text("ignored")
    (logs / "dir.jsonl").mkdir()
    make_junction(logs / "link.jsonl", tmp_path)
    journals = {j.name: j for j in list_journals(logs).journals}
    assert set(journals) == {
        "dir.jsonl",
        "link.jsonl",
        next(n for n in journals if n.startswith("run-")),
    }
    assert journals["dir.jsonl"].error() == journals["link.jsonl"].error()
    assert journals["link.jsonl"].error() == "Ссылка или не файл: не читается"
    assert journals["link.jsonl"].error(Lang.EN) == "A link or not a file: not read"


def test_a_linked_journal_directory_is_not_read(tmp_path: Path, make_junction) -> None:
    real = tmp_path / "real"
    write_journal(real)
    link = make_junction(tmp_path / "logs", real)
    listing = list_journals(link)
    assert listing.journals == () and "ссылка" in (listing.note_text() or "")


# --- R4-C: languages, custom extensions, skipped files --------------------------------------


def test_preview_texts_follow_the_language(src: Path) -> None:
    result = build_preview(settings(src), TABLE)
    rows = {r.name: r for r in result.rows(Lang.EN)}
    assert rows["b.jpg"].category == "🖼 Images" and rows["a.pdf"].action == "Move"
    assert rows["a.pdf"].source == str(src / "a.pdf")
    assert result.headline(Lang.EN).startswith("Preview ready")
    assert result.counts(Lang.EN).startswith("In the plan: 3. To be moved: 3")
    assert result.volume_lines(Lang.EN)[0].startswith("The source and the destination")
    assert effective_destination(str(src), None, Lang.EN).endswith("<category>")


def test_russian_volume_lines_are_those_of_volumeproof(src: Path) -> None:
    from downloads_organizer.volumeproof import describe_volume

    result = build_preview(settings(src), TABLE)
    assert result.plan is not None
    assert result.volume_lines() == describe_volume(result.plan.volume)


def test_errors_follow_the_language(src: Path) -> None:
    result = build_preview(settings(src, selected=frozenset()), TABLE)
    assert result.errors(Lang.EN) == ("No file type is selected",)
    assert result.headline(Lang.EN) == "Preview: the plan cannot be run"


def test_a_custom_extension_change_drops_the_preview(src: Path) -> None:
    from downloads_organizer.extensions import ExtensionTable

    model = model_for(src)
    previewed(model)
    assert model.set_table(ExtensionTable(custom={".avif": "Images"})) is True
    assert model.preview is None and not model.can_execute
    assert model.settings().custom == ((".avif", "Images"),)
    assert model.selection.selected_extensions == EXTS  # what still exists stays selected
    assert ".avif" not in model.selection.selected_extensions  # a new one is not selected


def test_a_custom_extension_is_planned_into_its_category(src: Path) -> None:
    from downloads_organizer.extensions import ExtensionTable

    touch(src, "photo.avif")
    model = model_for(src)
    model.set_table(ExtensionTable(custom={".avif": "Images"}))
    model.set_extension(".avif", True)
    result = previewed(model)
    row = next(r for r in result.rows() if r.name == "photo.avif")
    assert row.target == str(src / "DEL" / "Images" / "photo.avif")
    assert result.executable


def test_the_table_cannot_change_during_a_run(src: Path) -> None:
    from downloads_organizer.extensions import ExtensionTable

    model = model_for(src)
    previewed(model)
    model.start_run()
    with pytest.raises(RuntimeError):
        model.set_table(ExtensionTable(custom={".avif": "Images"}))


def change_mtime(path: Path) -> None:
    import os

    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000_000))


def test_a_file_changed_after_the_preview_is_skipped_not_failed(src: Path) -> None:
    model = model_for(src)
    previewed(model)
    plan = model.start_run()
    change_mtime(src / "a.pdf")
    outcome = execute(plan, model.table, no_cancel)
    model.finish_run()
    report = run_report(outcome)
    assert report.counts[ItemStatus.MOVED] == 2 and report.counts[ItemStatus.SKIPPED] == 1
    assert report.counts[ItemStatus.FAILED] == 0 and report.not_moved == 1
    assert (src / "a.pdf").is_file()  # not moved, for safety
    [row] = [r for r in report.rows if r.name == "a.pdf"]
    assert row.status == "Пропущено" and not row.attention
    assert row.status_code is ItemStatus.SKIPPED and row.reason_code is ExecReason.SOURCE_CHANGED
    assert row.reason == (
        "Файл изменился после предпросмотра. Перемещение отменено для безопасности"
        " (изменилось: время изменения)"
    )
    assert report.source_changed == 1
    changed = [line for line in report.lines if line.startswith("Изменились после предпросмотра")]
    assert changed == [
        "Изменились после предпросмотра: 1. Эти файлы не перемещены и остались на месте."
        " Нажмите «Обновить предпросмотр оставшихся», проверьте новый план и подтвердите"
        " перемещение заново."
    ]
    english = run_report(outcome, Lang.EN)
    [row] = [r for r in english.rows if r.name == "a.pdf"]
    assert row.status == "Skipped"
    assert row.reason == (
        "The file changed after the preview. It was not moved, for safety"
        " (changed: modification time)"
    )
    # a new preview covers the files that are left; it has to be confirmed again
    assert not model.can_execute
    again = previewed(model)
    assert [r.name for r in again.rows()] == ["a.pdf"] and model.can_execute


def test_an_unknown_detail_is_shown_as_it_is() -> None:
    assert appmodel.exec_text(ExecReason.SOURCE_CHANGED, "something else", Lang.EN).endswith(
        " (something else)"
    )
    assert appmodel.exec_text(ExecReason.SOURCE_CHANGED, "changed since the preview: x").endswith(
        "(changed since the preview: x)"
    )


def test_run_reports_follow_the_language() -> None:
    report = run_report(appmodel.RunOutcome(forbidden=True), Lang.EN)
    assert report.headline == "Execution is not allowed" and not report.changed_anything
    report = run_report(appmodel.RunOutcome(error="RuntimeError: x"), Lang.EN)
    assert report.headline == "The run was interrupted by an error" and report.changed_anything
    assert report.lines[0] == "RuntimeError: x"
    refusal = RunRefusal(None, RunRefusalReason.COPY_DISABLED, None)
    assert summarize_outcome(refusal, Lang.EN).headline == (
        "Run refused: Copying between volumes is disabled for now"
    )


def test_journal_details_follow_the_language(tmp_path: Path) -> None:
    path = write_journal(
        tmp_path / "logs",
        (Operation.MOVE, Status.INTENT, {"source": tmp_path / "a.pdf", "target": tmp_path / "b"}),
        finish=False,
    )
    journal = summarize_journal(path)
    russian, english = journal_details(journal), journal_details(journal, Lang.EN)
    assert "Итог: Не завершён" in russian and "перемещение:" in russian
    assert "Result: Not finished" in english and "move:" in english
    assert "Type: Move (schema v1)" in english


def test_format_time_by_language() -> None:
    from datetime import UTC, datetime

    moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert appmodel.format_time(moment).count(".") == 2
    assert appmodel.format_time(moment, Lang.EN).startswith("2026-01-0")


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("short", 10, "short"),
        ("abcdefghij", 10, "abcdefghij"),
        ("abcdefghijk", 10, "abcd…ghijk"),
        ("abc", 2, "abc"),
    ],
)
def test_shorten_middle(text: str, limit: int, expected: str) -> None:
    assert appmodel.shorten_middle(text, limit) == expected
    assert len(appmodel.shorten_middle(text, limit)) <= max(
        limit, len(text) if limit < 3 else limit
    )


# --- what the folder holds: in the plan and outside it (R4-D) ---------------------------


def test_preview_counts_every_file_and_groups_the_rest_by_extension(src: Path) -> None:
    touch(src, "s1.ps1", "s2.ps1", "tool.jar", "README")
    touch(src / "sub", "inner.pdf")
    preview = previewed(model_for(src))
    assert preview.inventory_counts() == "Файлов в папке: 8. В плане: 3. Вне плана: 5."
    assert preview.outside_lines() == (
        "Нет правила: .ps1 — 2, .jar — 1, без расширения — 1.",
        "Тип не выбран: .txt — 1.",
        "Чтобы включить их в план, выберите тип или добавьте расширение на вкладке"
        " «Типы файлов», затем сделайте новый предпросмотр.",
    )
    assert preview.inventory_details() == (
        "Считаются файлы только в самой папке, без вложенных папок; по ссылкам программа"
        " не переходит. Числа — на момент предпросмотра.",
        "Вложенных папок: 1 — они не открываются и не перемещаются.",
        "Вне плана по расширениям:",
        "  .ps1 — 2",
        "  .jar — 1",
        "  .txt — 1 (тип не выбран)",
        "  без расширения — 1",
    )
    assert preview.inventory_counts(Lang.EN) == (
        "Files in the folder: 8. In the plan: 3. Outside the plan: 5."
    )
    assert preview.outside_lines(Lang.EN)[0] == "No rule: .ps1 — 2, .jar — 1, no extension — 1."


def test_the_short_line_lists_the_largest_groups_and_sums_the_rest(src: Path) -> None:
    touch(src, *(f"f{i}.x{i}" for i in range(10)), "g1.big", "g2.big")
    lines = previewed(model_for(src)).outside_lines(limit=3)
    assert lines[0] == "Нет правила: .big — 2, .x0 — 1, .x1 — 1, ещё расширений: 8 (файлов: 8)."


def test_nothing_outside_the_plan_means_no_note(tmp_path: Path) -> None:
    folder = tmp_path / "only"
    touch(folder, "a.pdf", "b.jpg")
    preview = previewed(model_for(folder))
    assert preview.inventory_counts() == "Файлов в папке: 2. В плане: 2. Вне плана: 0."
    assert preview.outside_lines() == ()
    assert preview.summary_counts() == (
        "Файлов в папке: 2. В плане: 2. Вне плана: 0."
        " Будет перемещено: 2 (с новым именем: 0). Пропущено: 0."
    )
    assert preview.summary_counts(Lang.EN).count("In the plan") == 1


def test_without_a_listing_the_count_is_said_to_be_unknown(tmp_path: Path) -> None:
    preview = build_preview(settings(tmp_path / "absent"), TABLE)
    assert preview.inventory is None and preview.outside_lines() == ()
    assert preview.inventory_counts() == (
        "Сколько файлов в папке — неизвестно: папка не была прочитана."
    )
    assert preview.inventory_details() == ()
    assert preview.summary_counts() == (
        "Сколько файлов в папке — неизвестно: папка не была прочитана. " + preview.counts()
    )
    no_source = build_preview(settings(""), TABLE)
    assert no_source.inventory_counts() is None
    assert no_source.summary_counts() == no_source.counts()


def test_a_refused_volume_shows_only_how_many_files_there_are(
    src: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from downloads_organizer import winfileops

    monkeypatch.setattr(winfileops, "AVAILABLE", False)
    preview = build_preview(settings(src), TABLE)
    assert preview.plan is not None and preview.plan.errors and not preview.executable
    assert preview.inventory is not None and preview.inventory.files == 4
    assert preview.inventory_counts() == "Файлов в папке: 4."  # not «В плане: 3» next to 0
    assert preview.counts().startswith("В плане: 0.")
    assert preview.summary_counts() == "Файлов в папке: 4. " + preview.counts()


def test_run_counts_say_how_many_stayed_outside_the_plan(src: Path) -> None:
    touch(src, "s.ps1")
    model = model_for(src)
    preview = previewed(model)
    plan = model.start_run()
    outcome = execute(plan, model.table, no_cancel)
    model.finish_run()
    with_inventory = run_report(outcome, Lang.RU, preview.inventory)
    assert with_inventory.lines[0] == (
        "Перемещено: 3. Пропущено: 0. Ошибки: 0. Вне плана (при предпросмотре): 2."
    )
    assert run_report(outcome).lines[0] == "Перемещено: 3. Пропущено: 0. Ошибки: 0."
    assert sorted(p.name for p in src.iterdir() if p.is_file()) == ["notes.txt", "s.ps1"]


def test_new_custom_rules_bring_unclassified_files_into_a_new_plan(src: Path) -> None:
    touch(src, "a.gs", "b.ps1", "c.ps1", "tool.jar")
    model = model_for(src)
    first = previewed(model)
    assert first.inventory is not None
    assert {g.extension for g in first.inventory.outside} == {".gs", ".ps1", ".jar", ".txt"}
    table = model.table
    for ext in (".gs", ".ps1", ".jar"):
        table = table.with_custom(ext, "Code")
    model.set_table(table)
    assert not model.can_execute  # the old plan is gone with the old rules
    for ext in (".gs", ".ps1", ".jar"):
        model.set_extension(ext, True)
    second = previewed(model)
    assert second.inventory is not None and second.inventory.in_plan == 7
    assert [g.extension for g in second.inventory.outside] == [".txt"]
    assert {r.name for r in second.rows()} >= {"a.gs", "b.ps1", "c.ps1", "tool.jar"}
