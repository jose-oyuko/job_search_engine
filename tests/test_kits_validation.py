"""The fabrication and quality validator (spec §12.5) and the style rules (§12.4)."""

from typing import Any

import pytest

from apps.kits import style, validation
from apps.llm.schemas import CVData
from apps.profiles.serializers import serialize_profile
from tests.kit_helpers import Ids, good_cv, make_kit_user

pytestmark = pytest.mark.django_db

JOB_SKILLS = ["Helpdesk", "Active Directory", "Python"]


@pytest.fixture
def world() -> tuple[Any, Ids, dict[str, Any]]:
    user, ids = make_kit_user()
    return user, ids, serialize_profile(user)


def run(profile: dict[str, Any], cv: dict[str, Any], skills: list[str] | None = None) -> list:
    return validation.validate_cv(
        CVData.model_validate(cv), profile, job_skills=JOB_SKILLS if skills is None else skills
    )


def fields(issues: list) -> set[str]:
    return {i.field for i in issues}


def kinds(issues: list) -> set[str]:
    return {i.kind for i in issues}


# --- the happy path ---------------------------------------------------------------------------


def test_a_clean_cv_passes(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    assert run(profile, good_cv(ids)) == []


# --- check 1: IDs exist and belong to this user -------------------------------------------------


def test_an_unknown_id_is_caught(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    cv = good_cv(ids)
    cv["experiences"][0]["bullets"][0]["source_ids"] = ["BUL-99999"]
    issues = run(profile, cv)
    assert fields(issues) == {"experiences[0].bullets[0]"} and kinds(issues) == {validation.ID}


def test_another_users_id_is_caught(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    _, other = make_kit_user(full_name="Other Person")
    cv = good_cv(ids)
    cv["experiences"][0]["bullets"][1]["source_ids"] = [other.bul2]
    issues = run(profile, cv)
    assert fields(issues) == {"experiences[0].bullets[1]"} and kinds(issues) == {validation.ID}


@pytest.mark.parametrize("bad", ["", "EXP", "EXP-", "exp-1", "EXP-1; DROP", "XYZ-3", "12"])
def test_malformed_ids_are_unknown(world, bad: str) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    cv = good_cv(ids, skills=[bad])
    assert fields(run(profile, cv)) == {"skills[0]"}


def test_an_id_of_the_wrong_kind_is_caught(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    cv = good_cv(ids)
    cv["experiences"][0]["experience_id"] = ids.bul1  # a bullet used as a job
    cv["projects"][0]["project_id"] = ids.exp
    cv["skills"] = [ids.exp]
    cv["education_ids"] = [ids.crt]
    cv["certification_ids"] = [ids.edu]
    assert fields(run(profile, cv)) == {
        "experiences[0]",
        "projects[0]",
        "skills[0]",
        "education_ids[0]",
        "certification_ids[0]",
    }


def test_the_same_job_twice_is_caught(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    cv = good_cv(ids)
    cv["experiences"][1]["experience_id"] = ids.exp
    assert fields(run(profile, cv)) == {"experiences[1]"}


# --- check 2: every bullet has a source ---------------------------------------------------------


def test_a_bullet_without_sources_is_caught(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    cv = good_cv(ids)
    cv["experiences"][0]["bullets"][0]["source_ids"] = []
    issues = run(profile, cv)
    assert kinds(issues) == {validation.SOURCE}


def test_a_summary_without_sources_is_caught(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    assert fields(run(profile, good_cv(ids, summary_source_ids=[]))) == {"summary"}


# --- check 3: numbers ---------------------------------------------------------------------------


def bullet_cv(ids: Ids, text: str, source: str | None = None) -> dict[str, Any]:
    cv = good_cv(ids)
    cv["experiences"][0]["bullets"] = [{"text": text, "source_ids": [source or ids.bul1]}]
    return cv


def test_an_invented_number_is_caught(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    issues = run(profile, bullet_cv(ids, "Cut the backlog by 65%."))
    assert kinds(issues) == {validation.NUMBER} and "65" in issues[0].message


def test_a_number_from_the_cited_item_is_allowed(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    assert run(profile, bullet_cv(ids, "Cut the ticket backlog by 40%.")) == []


@pytest.mark.parametrize(
    "text", ["Cut the backlog by 40 percent.", "Cut the backlog by 40.0%.", "Backlog down 40%"]
)
def test_percent_forms_are_normalised(world, text: str) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    assert run(profile, bullet_cv(ids, text)) == []


def test_numbers_with_separators_are_normalised(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    assert validation.numbers_in("1,200 users and 3.50 hours") == {"1200", "3.5"}
    assert run(profile, bullet_cv(ids, "Supported 120 staff.")) == []


def test_a_number_from_an_item_that_is_not_cited_is_caught(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    issues = run(profile, bullet_cv(ids, "Cut the backlog by 40%.", source=ids.bul2))
    assert kinds(issues) == {validation.NUMBER}


def test_citing_the_job_allows_numbers_from_its_bullets(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    assert run(profile, bullet_cv(ids, "Cut the backlog by 40%.", source=ids.exp)) == []


def test_years_of_experience_may_appear_in_the_summary(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    cv = good_cv(ids, summary="Support specialist with 3 years of helpdesk work.")
    assert run(profile, cv) == []  # the Helpdesk skill has years=3
    assert fields(run(profile, good_cv(ids, summary="Support specialist with 9 years."))) == {
        "summary"
    }


# --- check 4: skills the user does not have -----------------------------------------------------


def test_a_job_skill_the_user_lacks_is_caught(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    issues = run(profile, bullet_cv(ids, "Managed Active Directory accounts."))
    assert kinds(issues) == {validation.SKILL} and "Active Directory" in issues[0].message


def test_a_job_skill_the_user_has_is_allowed(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    assert run(profile, bullet_cv(ids, "Worked on the helpdesk for 120 staff.")) == []


def test_skill_matching_ignores_case_and_simple_plurals(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    assert run(profile, bullet_cv(ids, "Supported HELPDESKS for 120 staff.")) == []
    issues = run(profile, bullet_cv(ids, "Supported ACTIVE DIRECTORY for 120 staff."))
    assert kinds(issues) == {validation.SKILL}


def test_a_skill_named_in_the_cited_item_text_is_allowed(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    cv = bullet_cv(ids, "Wrote guides on Windows laptops.", source=ids.bul2)
    assert run(profile, cv, skills=["Windows laptops"]) == []


def test_a_skill_in_the_item_text_that_is_not_cited_is_caught(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    cv = bullet_cv(ids, "Set up Windows laptops.", source=ids.bul1)
    assert kinds(run(profile, cv, skills=["Windows"])) == {validation.SKILL}


def test_short_skill_names_are_not_matched_loosely(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    cv = bullet_cv(ids, "Set up Windows laptops for 120 staff.", source=ids.bul1)
    assert run(profile, cv, skills=["C", "Go"]) == []


def test_skill_ids_must_be_the_users_own(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    _, other = make_kit_user(full_name="Other Person")
    assert fields(run(profile, good_cv(ids, skills=[other.skl_python]))) == {"skills[0]"}


# --- check 5: banned phrases and em dashes -------------------------------------------------------


@pytest.mark.parametrize("label", list(style.BANNED_PHRASES))
def test_every_banned_phrase_is_flagged(label: str) -> None:
    samples = {
        "leverage": "We leveraged tools",
        "spearheaded": "She spearheaded it",
        "synergy": "Great synergy",
        "dynamic": "A dynamic team",
        "passionate": "I am passionate",
        "results-driven": "A results-driven person",
        "proven track record": "A proven track record",
        "go-getter": "A go-getter",
        "cutting-edge": "Cutting-edge tools",
        "seamless": "Seamlessly done",
        "robust": "A robust setup",
        "detail-oriented professional": "A detail-oriented professional",
        "I am excited to apply": "I am excited to apply for this",
        "I am writing to express my interest": "I am writing to express my interest in it",
        "thrilled": "I am thrilled",
        "delve": "Let me delve in",
        "tapestry": "A rich tapestry",
        "in today's fast-paced": "In today's fast-paced world",
        "hit the ground running": "I will hit the ground running",
        "team player": "A great team player",
        "think outside the box": "I think outside the box",
        "utilize": "I utilize tools",
    }
    assert label in style.find_banned(samples[label])


def test_banned_words_match_whole_words_only() -> None:
    assert style.find_banned("The dynamics of a large team, and a robotic arm") == []
    assert style.find_banned("UTILISED and Leveraging") == ["leverage", "utilize"]


def test_a_banned_phrase_in_a_bullet_is_caught(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    issues = run(profile, bullet_cv(ids, "Leveraged helpdesk tools for 120 staff."))
    assert kinds(issues) == {validation.STYLE}


@pytest.mark.parametrize("dash", ["—", "―"])
def test_an_em_dash_is_caught(world, dash: str) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    issues = run(profile, bullet_cv(ids, f"Fixed laptops {dash} for 120 staff."))
    assert [i.message for i in issues] == ["contains an em dash"]


def test_an_en_dash_or_hyphen_is_fine(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    assert run(profile, bullet_cv(ids, "Fixed laptops (on-site – and remote) for 120 staff.")) == []


def test_style_applies_to_headline_and_summary(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    cv = good_cv(ids, headline="Passionate support person", summary="Robust helpdesk skills.")
    assert fields(run(profile, cv)) == {"headline", "summary"}


# --- check 6: length limits -----------------------------------------------------------------------


def test_a_bullet_of_31_words_is_too_long(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    ok = " ".join(["word"] * 29) + " 40%"  # 30 words
    assert run(profile, bullet_cv(ids, ok)) == []
    issues = run(profile, bullet_cv(ids, ok + " more"))
    assert kinds(issues) == {validation.LENGTH}


def test_a_summary_over_60_words_is_too_long(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    assert run(profile, good_cv(ids, summary=" ".join(["helpdesk"] * 60))) == []
    assert kinds(run(profile, good_cv(ids, summary=" ".join(["helpdesk"] * 61)))) == {
        validation.LENGTH
    }


def test_more_than_14_skills_is_too_many(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    assert "skills" in fields(run(profile, good_cv(ids, skills=[ids.skl_excel] * 15)))


def test_bullet_count_limits(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    cv = good_cv(ids)
    cv["experiences"][0]["bullets"] = [
        {"text": "Set up laptops.", "source_ids": [ids.bul2]} for _ in range(6)
    ]
    cv["projects"] = [
        {
            "project_id": ids.prj,
            "bullets": [{"text": "Built a tool.", "source_ids": [ids.prj]} for _ in range(4)],
        }
    ]
    assert fields(run(profile, cv)) == {"experiences[0]", "projects[0]"}


def test_more_than_three_projects_is_too_many(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    project = good_cv(ids)["projects"][0]
    assert "projects" in fields(run(profile, good_cv(ids, projects=[project] * 4)))


def test_an_empty_bullet_is_caught(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    assert kinds(run(profile, bullet_cv(ids, "   "))) == {validation.LENGTH}


# --- repair: what survives -----------------------------------------------------------------------


def drop(world_: tuple[Any, Ids, dict[str, Any]], cv: dict[str, Any]) -> validation.Repaired:
    _, _, profile = world_
    issues = run(profile, cv)
    return validation.drop_failing(
        CVData.model_validate(cv),
        issues,
        fallback_headline="Own headline",
        fallback_summary="Own summary.",
    )


def test_failing_bullets_are_dropped_and_good_ones_kept(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, _ = world
    cv = good_cv(ids)
    cv["experiences"][0]["bullets"].append({"text": "Cut costs by 77%.", "source_ids": [ids.bul2]})
    result = drop(world, cv)
    texts = [b.text for b in result.cv.experiences[0].bullets]
    assert texts == [
        "Resolved support tickets for 120 staff and cut the backlog by 40%.",
        "Set up Windows laptops for new staff.",
    ]
    assert [i.field for i in result.dropped] == ["experiences[0].bullets[2]"]


def test_an_unknown_job_is_removed_but_a_job_that_lost_its_bullets_stays(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, _ = world
    cv = good_cv(ids)
    cv["experiences"].insert(0, {"experience_id": "EXP-99999", "bullets": []})
    cv["experiences"][2]["bullets"] = [{"text": "Won 99 awards.", "source_ids": [ids.bul3]}]
    result = drop(world, cv)
    assert [e.experience_id for e in result.cv.experiences] == [ids.exp, ids.exp2]
    assert result.cv.experiences[1].bullets == []  # the header is a fact from the profile


def test_an_em_dash_only_problem_is_repaired_not_dropped(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, _ = world
    cv = bullet_cv(ids, "Fixed laptops — for 120 staff.")
    cv["headline"] = "IT support — Helpdesk"
    result = drop(world, cv)
    assert result.cv.experiences[0].bullets[0].text == "Fixed laptops, for 120 staff."
    assert result.cv.headline == "IT support, Helpdesk"
    assert result.dropped == [] and result.repaired


def test_an_em_dash_with_another_problem_is_dropped(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, _ = world
    result = drop(world, bullet_cv(ids, "Fixed it — saving 99%."))
    assert result.cv.experiences[0].bullets == []


def test_a_bad_headline_and_summary_fall_back_to_the_users_words(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, _ = world
    cv = good_cv(ids, headline="Passionate expert", summary="A robust professional.")
    result = drop(world, cv)
    assert result.cv.headline == "Own headline"
    assert result.cv.summary == "Own summary." and result.cv.summary_source_ids == []


def test_unknown_skills_education_and_certifications_are_removed(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, _ = world
    cv = good_cv(
        ids,
        skills=[ids.skl_excel, "SKL-99999"],
        education_ids=["EDU-99999", ids.edu],
        certification_ids=["CRT-99999"],
    )
    result = drop(world, cv)
    assert result.cv.skills == [ids.skl_excel]
    assert result.cv.education_ids == [ids.edu] and result.cv.certification_ids == []


def test_too_many_bullets_and_skills_are_trimmed(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, _ = world
    cv = good_cv(ids, skills=[ids.skl_excel] * 16)
    cv["experiences"][0]["bullets"] = [
        {"text": f"Set up laptops {i}.", "source_ids": [ids.bul2]} for i in range(7)
    ]
    # the numbers 0-6 are not in the source, so those bullets go; trimming applies to the rest
    result = drop(world, cv)
    assert len(result.cv.skills) <= validation.MAX_SKILLS
    assert len(result.cv.experiences[0].bullets) <= validation.MAX_EXPERIENCE_BULLETS


def test_what_survives_a_repair_passes_validation(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, profile = world
    cv = good_cv(ids, headline="Spearheaded things", summary_source_ids=["EXP-99999"])
    cv["experiences"][0]["bullets"] += [
        {"text": "Cut cost by 91%.", "source_ids": [ids.bul2]},
        {"text": "Managed Active Directory.", "source_ids": [ids.bul2]},
        {"text": "No source.", "source_ids": []},
        {"text": "Robust fixes.", "source_ids": [ids.bul2]},
    ]
    result = drop(world, cv)
    leftover = run(profile, result.cv.model_dump())
    # The only thing left is the fallback summary, which is the user's own text and has no item ID.
    assert [(i.field, i.kind) for i in leftover] == [("summary", validation.SOURCE)]
    assert result.cv.summary == "Own summary."


def test_the_original_cv_is_not_mutated(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, _ = world
    original = CVData.model_validate(bullet_cv(ids, "Cut it by 91%."))
    validation.drop_failing(
        original,
        [validation.Issue("experiences[0].bullets[0]", "x", validation.NUMBER)],
        fallback_headline="",
        fallback_summary="",
    )
    assert len(original.experiences[0].bullets) == 1


# --- the report ----------------------------------------------------------------------------------


def test_a_clean_report_passes() -> None:
    report = validation.build_report([])
    assert report["passed"] is True and report["issues"] == []


def test_a_report_with_removals_does_not_pass(world) -> None:  # type: ignore[no-untyped-def]
    _, ids, _ = world
    result = drop(world, bullet_cv(ids, "Cut it by 91%."))
    report = validation.build_report([], result, retried=True, notes=["note"])
    assert report["passed"] is False and report["retried"] is True and report["notes"] == ["note"]
    assert report["issues"][0]["field"] == "experiences[0].bullets[0]"


# --- cover letters and edits ------------------------------------------------------------------------


def words(n: int) -> str:
    return " ".join(["word"] * n)


def test_a_letter_of_the_right_length_passes() -> None:
    assert validation.check_letter(words(200), allowed_numbers=set()) == []


def test_letter_length_limits() -> None:
    assert validation.check_letter(words(179), allowed_numbers=set())[0].message.startswith(
        "is shorter"
    )
    assert validation.check_letter(words(301), allowed_numbers=set())[0].message.startswith(
        "is longer"
    )


def test_a_letter_with_invented_numbers_or_banned_words_is_flagged() -> None:
    text = words(200) + " I have 15 years. I am passionate."
    messages = [i.message for i in validation.check_letter(text, allowed_numbers={"3"})]
    assert any("15" in m for m in messages) and any("passionate" in m for m in messages)
    assert validation.check_letter(text, allowed_numbers={"15"})[0].kind == validation.STYLE


def test_edits_only_produce_warnings() -> None:
    issues = validation.check_edit(
        [("headline", "A robust setup"), ("summary", "Fine"), ("b", "x — y")]
    )
    assert [i.field for i in issues] == ["headline", "b"]
