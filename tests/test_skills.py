from arty_cli import skills, tools


def make_skill(folder, name, text, extra_files=()):
    skill_folder = folder / name
    skill_folder.mkdir(parents=True)
    (skill_folder / skills.SKILL_FILE).write_text(text)
    for extra in extra_files:
        (skill_folder / extra).write_text("x")
    return skill_folder


def places(tmp_path):
    personal = tmp_path / "personal"
    project = tmp_path / "project"
    project.mkdir()
    return personal, tools.Workspace(project)


def test_skills_are_found_with_their_names_and_descriptions(tmp_path):
    personal, workspace = places(tmp_path)
    make_skill(personal / skills.SKILLS_FOLDER, "release-notes",
               "---\nname: release-notes\ndescription: Write release notes from git log\n---\n# Steps\n1. Read git log\n")
    make_skill(workspace.root / skills.PROJECT_SKILLS, "deploy",
               "---\ndescription: \"How this project deploys\"\n---\nRun make deploy.\n", ["deploy.sh"])
    found = skills.find(personal, workspace, include_project=True)
    assert {name: (skill.description, skill.source) for name, skill in found.items()} == {
        "release-notes": ("Write release notes from git log", "yours"),
        "deploy": ("How this project deploys", "this project")}
    assert "release-notes: Write release notes from git log" in skills.listing(found)


def test_project_skills_need_a_trusted_folder(tmp_path):
    personal, workspace = places(tmp_path)
    make_skill(workspace.root / skills.PROJECT_SKILLS, "deploy", "---\ndescription: d\n---\nbody\n")
    assert skills.find(personal, workspace, include_project=False) == {}


def test_loading_a_skill_gives_its_instructions_and_files(tmp_path):
    personal, workspace = places(tmp_path)
    folder = make_skill(workspace.root / skills.PROJECT_SKILLS, "deploy",
                        "---\ndescription: d\n---\nRun make deploy.\n", ["deploy.sh"])
    found = skills.find(personal, workspace, include_project=True)
    loaded = skills.loaded(found, "deploy")
    assert "Run make deploy." in loaded and "description" not in loaded
    assert str(folder) in loaded and "deploy.sh" in loaded


def test_an_unknown_skill_lists_the_real_ones(tmp_path):
    personal, workspace = places(tmp_path)
    make_skill(personal / skills.SKILLS_FOLDER, "a", "---\ndescription: d\n---\nx\n")
    assert skills.loaded(skills.find(personal, workspace, False), "b") == "Error: there is no skill called b. Skills: a"


def test_a_skill_without_front_matter_uses_its_folder_name_and_first_line(tmp_path):
    personal, workspace = places(tmp_path)
    make_skill(personal / skills.SKILLS_FOLDER, "plain", "\nTidy imports before committing.\nMore.\n")
    found = skills.find(personal, workspace, False)
    assert found["plain"].description == "Tidy imports before committing."


def test_folders_without_a_skill_file_are_ignored(tmp_path):
    personal, workspace = places(tmp_path)
    (personal / skills.SKILLS_FOLDER / "empty").mkdir(parents=True)
    assert skills.find(personal, workspace, False) == {} and skills.listing({}) == ""
