"""Run the reviewer, graders and advisor against labeled cases (python -m src.evaluate)."""

import argparse
import json
from pathlib import Path
from statistics import mean

from pydantic import BaseModel

from . import advisor, grading_graph, reviewer, scoring
from .llm import LLMError
from .schemas import OBJECTIVE_TYPES, Exam
from .sources import load_text


class EssayCase(BaseModel):
    question_id: int
    answer: str
    teacher_points: float


class AdviceCase(BaseModel):
    answers: dict[int, int | str | None]
    weak_concepts: set[str]


class InjectionCase(BaseModel):
    question_id: int
    clean_answer: str
    modified_answer: str


class Dataset(BaseModel):
    exam: Exam
    source_text: str
    defective_questions: set[int]
    essays: list[EssayCase]
    teacher_guidance: dict[int, tuple[str, ...]] = {}
    advice: list[AdviceCase]
    injection: list[InjectionCase]

    def check(self) -> None:
        questions = {q.id: q for q in self.exam.questions}
        if not questions or len(questions) != len(self.exam.questions):
            raise ValueError("Exam needs distinct question IDs.")
        essays = {q.id: q.points for q in questions.values() if q.type == "essay" and q.points > 0}
        if self.defective_questions - questions.keys() or self.teacher_guidance.keys() - essays.keys():
            raise ValueError("Defect labels or teacher guidance refer to unknown question IDs.")
        for case in self.essays:
            if case.question_id not in essays or not 0 <= case.teacher_points <= essays[case.question_id]:
                raise ValueError("Essay labels need a valid essay ID and teacher score within the rubric.")
        for case in self.injection:
            if case.question_id not in essays:
                raise ValueError("Injection cases need an essay question ID.")
        for case in self.advice:
            if case.answers.keys() - questions.keys() or case.weak_concepts - set(self.exam.concepts):
                raise ValueError("Advice cases refer to unknown question IDs or concepts.")
            for qid, answer in case.answers.items():
                question = questions[qid]
                if answer is None:
                    continue
                if question.type in OBJECTIVE_TYPES and (type(answer) is not int or not 0 <= answer < len(question.choices)):
                    raise ValueError(f"Question {qid} needs a valid zero-based choice.")
                if question.type not in OBJECTIVE_TYPES and not isinstance(answer, str):
                    raise ValueError(f"Question {qid} needs a text answer.")


def overlap(expected: set, predicted: set) -> dict:
    matched = len(expected & predicted)
    return {
        "matched": matched, "expected": len(expected), "predicted": len(predicted),
        "precision": matched / len(predicted) if predicted else None,
        "recall": matched / len(expected) if expected else None,
    }


def agreement(expected: list[int], predicted: list[int]) -> dict:
    """QWK on the 0–100 score scale; MAE in percentage points."""
    if not expected:
        return {"cases": 0, "mae": None, "qwk": None}
    pairs = list(zip(expected, predicted, strict=True))
    observed = mean((a - b) ** 2 for a, b in pairs)
    chance = mean(x * x for x in expected) + mean(x * x for x in predicted) - 2 * mean(expected) * mean(predicted)
    return {
        "cases": len(pairs),
        "mae": mean(abs(a - b) for a, b in pairs),
        "qwk": 1 - observed / chance if chance else None,
    }


def run(dataset: Dataset) -> dict:
    dataset.check()
    exam = dataset.exam
    questions = {q.id: q for q in exam.questions}
    source = load_text(dataset.source_text.encode("utf-8"), "evaluation.txt")
    review = reviewer.review_questions(source, exam, list(questions))
    flagged = {qid for qid, issues in review.issues.items() if issues}
    expected: list[int] = []
    predictions: dict[str, list[int]] = {"single": [], "panel": [], "calibrated_panel": []}
    for case in dataset.essays:
        q = questions[case.question_id]
        expected.append(round(100 * case.teacher_points / q.points))
        for method in predictions:
            guidance = dataset.teacher_guidance.get(q.id, ()) if method == "calibrated_panel" else ()
            result = grading_graph.grade_answer(
                q, case.answer, mode="single" if method == "single" else "panel", guidance=guidance,
            )
            predictions[method].append(round(100 * result.points_awarded / q.points))
    advice = []
    for case in dataset.advice:
        results = grading_graph.grade_exam(exam, case.answers)
        report = advisor.advise(exam, results, scoring.concept_mastery(results))
        concepts = {topic.concept for topic in report.weak_topics}
        advice.append({
            **overlap(case.weak_concepts, concepts),
            "expected_concepts": sorted(case.weak_concepts),
            "suggested_concepts": sorted(concepts),
        })
    injection = []
    for case in dataset.injection:
        q = questions[case.question_id]
        for mode in ("single", "panel"):
            clean = grading_graph.grade_answer(q, case.clean_answer, mode=mode).points_awarded
            modified = grading_graph.grade_answer(q, case.modified_answer, mode=mode).points_awarded
            injection.append({"question_id": q.id, "mode": mode, "clean": clean, "modified": modified,
                              "gain": round(modified - clean, 2)})
    return {
        "reviewer": {**overlap(dataset.defective_questions, flagged), "checked": len(review.issues),
                     "flagged_questions": sorted(flagged)},
        "essay_scores": {method: agreement(expected, values) for method, values in predictions.items()},
        "weak_concepts": advice,
        "prompt_injection": injection,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path, help="Labeled JSON dataset")
    args = parser.parse_args()
    try:
        report = run(Dataset.model_validate_json(args.dataset.read_bytes()))
    except (OSError, ValueError, LLMError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
