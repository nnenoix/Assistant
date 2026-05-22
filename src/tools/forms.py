"""Google Forms tools.

Requires OAuth scope `forms.body` (create/edit form structure) +
`forms.responses.readonly` (read submissions). GCP project needs
`forms.googleapis.com` enabled.
"""
from functools import lru_cache

from googleapiclient.discovery import build

from src.auth import RetryingHttpRequest, get_credentials


DEFAULT_ACCOUNT = "main"


@lru_cache(maxsize=8)
def _service(account: str = DEFAULT_ACCOUNT):
    return build(
        "forms", "v1",
        credentials=get_credentials(account),
        cache_discovery=False,
        requestBuilder=RetryingHttpRequest,
    )


def create(
    title: str,
    description: str | None = None,
    parent_folder_id: str | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Create a new Google Form. Returns {form_id, title, url, edit_url}.

    Forms API limitation: at creation time only `info.title` and
    `info.documentTitle` are accepted. Description must be added via a
    subsequent batchUpdate (updateFormInfo). We do this here so the caller
    gets a single-call experience.
    """
    body = {"info": {"title": title, "documentTitle": title}}
    resp = _service(account).forms().create(body=body).execute()
    form_id = resp["formId"]
    if description:
        _service(account).forms().batchUpdate(
            formId=form_id,
            body={"requests": [{
                "updateFormInfo": {
                    "info": {"description": description},
                    "updateMask": "description",
                },
            }]},
        ).execute()
    if parent_folder_id:
        from src.tools import drive as _drive
        try:
            _drive.move(form_id, parent_folder_id, account=account)
        except Exception:
            pass
    return {
        "form_id": form_id,
        "title": title,
        "url": resp.get("responderUri") or f"https://docs.google.com/forms/d/{form_id}/viewform",
        "edit_url": f"https://docs.google.com/forms/d/{form_id}/edit",
    }


_QUESTION_TYPES = {"text", "paragraph", "multiple_choice", "checkbox", "dropdown", "scale", "date"}


def add_question(
    form_id: str,
    question_type: str,
    title: str,
    *,
    required: bool = False,
    options: list[str] | None = None,
    scale_low: int = 1,
    scale_high: int = 5,
    scale_low_label: str | None = None,
    scale_high_label: str | None = None,
    paragraph: bool = False,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Append a question to the form.

    question_type:
      - "text" — single-line text (paragraph=True → multi-line).
      - "paragraph" — multi-line text.
      - "multiple_choice" — single-select radio; needs `options`.
      - "checkbox" — multi-select; needs `options`.
      - "dropdown" — single-select dropdown; needs `options`.
      - "scale" — 1..N linear scale; `scale_low`/`scale_high` + labels.
      - "date" — date picker.
    """
    if question_type not in _QUESTION_TYPES:
        raise ValueError(f"unknown question_type {question_type!r}; allowed: {sorted(_QUESTION_TYPES)}")

    question: dict = {"required": required}
    if question_type == "text" or question_type == "paragraph":
        question["textQuestion"] = {"paragraph": question_type == "paragraph" or paragraph}
    elif question_type in ("multiple_choice", "checkbox", "dropdown"):
        if not options:
            raise ValueError(f"{question_type} needs `options` list")
        type_map = {"multiple_choice": "RADIO", "checkbox": "CHECKBOX", "dropdown": "DROP_DOWN"}
        question["choiceQuestion"] = {
            "type": type_map[question_type],
            "options": [{"value": o} for o in options],
        }
    elif question_type == "scale":
        scale = {"low": scale_low, "high": scale_high}
        if scale_low_label:
            scale["lowLabel"] = scale_low_label
        if scale_high_label:
            scale["highLabel"] = scale_high_label
        question["scaleQuestion"] = scale
    elif question_type == "date":
        question["dateQuestion"] = {"includeTime": False, "includeYear": True}

    # Need current items to know append index
    form = _service(account).forms().get(formId=form_id).execute()
    current_count = len(form.get("items", []))

    resp = _service(account).forms().batchUpdate(
        formId=form_id,
        body={"requests": [{
            "createItem": {
                "item": {
                    "title": title,
                    "questionItem": {"question": question},
                },
                "location": {"index": current_count},
            },
        }]},
    ).execute()
    item_id = resp.get("replies", [{}])[0].get("createItem", {}).get("itemId")
    return {
        "ok": True,
        "form_id": form_id,
        "item_id": item_id,
        "question_type": question_type,
        "position": current_count,
    }


def read(form_id: str, account: str = DEFAULT_ACCOUNT) -> dict:
    """Read a form's title, description, and full question list.

    Returns {title, description, questions, _meta}.
    """
    form = _service(account).forms().get(formId=form_id).execute()
    info = form.get("info", {})
    items = form.get("items", []) or []
    questions = []
    for item in items:
        qi = item.get("questionItem", {})
        q = qi.get("question", {})
        kind = "unknown"
        if "textQuestion" in q:
            kind = "paragraph" if q["textQuestion"].get("paragraph") else "text"
        elif "choiceQuestion" in q:
            tmap = {"RADIO": "multiple_choice", "CHECKBOX": "checkbox", "DROP_DOWN": "dropdown"}
            kind = tmap.get(q["choiceQuestion"].get("type"), "choice")
        elif "scaleQuestion" in q:
            kind = "scale"
        elif "dateQuestion" in q:
            kind = "date"
        questions.append({
            "item_id": item.get("itemId"),
            "title": item.get("title"),
            "kind": kind,
            "required": q.get("required", False),
        })
    return {
        "title": info.get("title"),
        "description": info.get("description"),
        "questions": questions,
        "_meta": {
            "form_id": form_id,
            "question_count": len(questions),
            "empty_reason": None if questions else "no_questions",
        },
    }


def read_responses(
    form_id: str,
    since: str | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict:
    """Read responses (submissions) to a form. Returns {responses, _meta}.

    `since` filter is an RFC3339 timestamp — only responses submitted after
    that time are returned. Pass None to fetch everything.
    """
    filter_param = None
    if since:
        filter_param = f"timestamp > {since}"
    kwargs = {"formId": form_id}
    if filter_param:
        kwargs["filter"] = filter_param
    resp = _service(account).forms().responses().list(**kwargs).execute()
    raw = resp.get("responses", []) or []
    out = []
    for r in raw:
        answers = {}
        for ans_id, ans in (r.get("answers") or {}).items():
            ta = ans.get("textAnswers", {}).get("answers", []) or []
            answers[ans_id] = [a.get("value") for a in ta]
        out.append({
            "response_id": r.get("responseId"),
            "submitted_at": r.get("lastSubmittedTime"),
            "answers": answers,
        })
    return {
        "responses": out,
        "_meta": {
            "form_id": form_id,
            "count": len(out),
            "since": since,
            "empty_reason": None if out else "no_responses",
        },
    }
