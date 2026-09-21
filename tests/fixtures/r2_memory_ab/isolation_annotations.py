"""Predeclared labels/selection for the isolation experiment, never final gold.

Rows refer to first MEMORY calls in the old V5.1 128-sample run. Only query,
global question and visible facts are imported; model answers are not read.
Their one-query R2 projection is a new isolated fixture, NOT an R2 trajectory.
"""
HELDOUT = [
    # row, sample, query ID, value, directly supporting visible fact positions
    (3, "dev_3867", "Q1", None, []),
    (11, "dev_4969", "Q1", None, []),
    (30, "dev_11816", "Q1", 1965, [0, 1]),
    (37, "dev_6489", "Q1", 2007, [0]),
    (52, "dev_2539", "Q1", "Vittorio Cottafavi", [0]),
    (86, "dev_324", "Q1", "Åke Ohberg", [0]),
    (98, "dev_6579", "Q2", "J. Mahendran", [1]),
    (108, "dev_9001", "Q1", "George Compton, 4th Earl of Northampton", [0]),
    (117, "dev_4741", "Q1", "John Roberts", [0]),
    (125, "dev_12536", "Q1", "Otis Turner", [0]),
    (185, "dev_4533", "Q1", "Louis Nero", [0]),
    (229, "dev_10497", "Q1", None, []),
]

REPEATS = 3
SELECTION_RULE = (
    "Before held-out calls, select P17-full or P17-local by family-macro exact "
    "accuracy on the shared stage-1 core (all three repeats). Require zero false "
    "bindings and no unresolved API failures. Ties prefer P17-local. Compare to "
    "B on the same core; record whether gain exists. Regardless of diagnostic "
    "outcome, do not edit this selected prompt after selection. Stage 2 uses B "
    "and the frozen selection, not retuned prompts. Production default stays B."
)
