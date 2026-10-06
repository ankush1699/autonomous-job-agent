from typing import TypedDict, Any, List

class ResumeGraphState(TypedDict):
    company_name: str
    job_title: str
    job_description: str
    master_data: dict

    generate_resume: bool
    generate_cover_letter: bool
    one_page: bool

    jd_keywords: List[str]
    verified_keywords: List[str]   # keywords confirmed present in master data
    gap_keywords: List[str]        # keywords in JD but absent from master data

    should_apply: Any              # verdict dict from core.should_apply (stage, score, red_flags, ...)
    triage_score: int              # mirrors should_apply score (kept for metadata/back-compat)
    triage_reasoning: str
    proceed: bool

    selected_content: Any
    selection_report: Any          # dropped bullets, scores, content warnings
    trust_report: Any              # pre-editor Python validation output
    final_resume_data: Any

    resume_strategy: Any           # structured strategy object saved to strategy.json

    output_folder: str
    pipeline_status: str
