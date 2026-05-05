import html
import json
import logging
import os
import re
import sys
import threading
import traceback
import unicodedata
import uuid
import webbrowser
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import certifi
import requests


PORT = 8765
APP_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(__file__)
STATE_FILE = os.path.join(APP_DIR, "sync_state.json")
REPORT_FILE = os.path.join(APP_DIR, "current_xml_report.json")
UPLOAD_DIR = os.path.join(APP_DIR, "uploads")
CONFIG_FILE = os.path.join(APP_DIR, "app_config.json")
LOG_FILE = os.path.join(APP_DIR, "app.log")
DEFAULT_ASAAS_BASE_URL = os.environ.get("ASAAS_BASE_URL", "https://api.asaas.com/v3")
DEFAULT_XML_PATH = ""
APP_STATE: Dict[str, Any] = {
    "xml_path": "",
    "records": [],
    "records_by_reference": {},
    "summary": {},
    "messages": [],
    "snapshot_missing": [],
    "snapshot_added": [],
}
SYNC_JOBS: Dict[str, Dict[str, Any]] = {}
SYNC_JOBS_LOCK = threading.Lock()


logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def configure_ssl_environment() -> str:
    bundled_path = os.path.join(APP_DIR, "_internal", "certifi", "cacert.pem")
    ca_bundle = bundled_path if os.path.exists(bundled_path) else certifi.where()
    os.environ["SSL_CERT_FILE"] = ca_bundle
    logger.info("CA bundle configurado: %s", ca_bundle)
    return ca_bundle


CA_BUNDLE_PATH = configure_ssl_environment()


@dataclass
class ChargeRecord:
    external_reference: str
    source_key: str
    conta_receber_id: str
    numero_matricula: str
    numero_parcela: str
    aluno: str
    responsavel: str
    payer_source: str
    payer_name: str
    payer_cpf_cnpj: str
    payer_email: str
    payer_phone: str
    due_date: str
    categoria: str
    turma: str
    bolsa_label: str
    desconto_percentual: Optional[float]
    desconto_confiavel: bool
    valor_cheio: float
    valor_final: float
    valor_original_xml: float
    valor_liquido_xml: float
    valor_com_desconto_xml: float
    forma_cobranca: str
    situacao: str
    ready_to_sync: bool
    blocking_reason: str
    raw: Dict[str, Any]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def empty_state() -> Dict[str, Any]:
    return {
        "payments_by_reference": {},
        "xml_snapshots_by_period": {},
    }


def ensure_state_defaults(state: Dict[str, Any]) -> Dict[str, Any]:
    state.setdefault("payments_by_reference", {})
    state.setdefault("xml_snapshots_by_period", {})
    return state


def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return empty_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        if isinstance(data, dict):
            return ensure_state_defaults(data)
    except Exception:
        pass
    return empty_state()


def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)


def save_report(records: List[ChargeRecord], summary: Dict[str, Any], xml_path: str) -> None:
    payload = {
        "generated_at": now_iso(),
        "xml_path": xml_path,
        "summary": summary,
        "records": [asdict(record) for record in records],
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)


def add_message(kind: str, text: str) -> None:
    logger.info("UI message | %s | %s", kind.upper(), text)
    APP_STATE.setdefault("messages", []).append({"kind": kind, "text": text})


def pop_messages() -> List[Dict[str, str]]:
    messages = APP_STATE.get("messages", [])
    APP_STATE["messages"] = []
    return messages


def only_digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def normalize_label(value: str) -> str:
    base = unicodedata.normalize("NFKD", value or "")
    base = "".join(ch for ch in base if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", base).strip().lower()


def parse_brazilian_percent(value: str) -> Optional[float]:
    text = (value or "").strip().replace("%", "").replace(",", ".")
    if not text:
        return None
    try:
        parsed = round(float(text), 2)
        return parsed if 0 < parsed < 100 else None
    except Exception:
        return None


def parse_money(value: str) -> float:
    text = (value or "").strip()
    if not text:
        return 0.0
    text = text.replace(".", ".").replace(",", ".")
    try:
        return round(float(text), 2)
    except Exception:
        return 0.0


def parse_date(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    return text[:10]


def escape(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def format_currency(value: float) -> str:
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def infer_discount_percent(valor_cheio: float, valor_final: float, bolsa_label: str) -> Tuple[Optional[float], bool]:
    bolsa_percent = parse_brazilian_percent(bolsa_label)
    if bolsa_percent and valor_cheio > 0:
        calculado = round(valor_cheio * (1 - (bolsa_percent / 100)), 2)
        if abs(calculado - valor_final) <= 0.05:
            return bolsa_percent, True
    return None, False


def choose_value(valor_liquido: float, valor_com_desconto: float, valor: float) -> float:
    for current in (valor_liquido, valor_com_desconto, valor):
        if current > 0:
            return round(current, 2)
    return 0.0


def choose_payer(raw: Dict[str, str]) -> Tuple[str, str, str, str, str]:
    responsavel_nome = (raw.get("NomeResponsavel") or "").strip()
    responsavel_cpf = only_digits(raw.get("CPFResponsavel") or "")
    responsavel_email = (raw.get("EmailResponsavel") or "").strip()
    responsavel_phone = only_digits(raw.get("CelularResponsavel") or raw.get("FoneResponsavel") or raw.get("FoneComercialResponsavel") or "")
    has_responsavel = any([responsavel_nome, responsavel_cpf, responsavel_email, responsavel_phone])

    if has_responsavel:
        return (
            "responsavel",
            responsavel_nome,
            responsavel_cpf,
            responsavel_email,
            responsavel_phone,
        )

    aluno_nome = (raw.get("Sacado") or "").strip()
    aluno_phone = only_digits(raw.get("Celular") or raw.get("Telefone") or "")
    return ("aluno", aluno_nome, "", "", aluno_phone)


def build_external_reference(raw: Dict[str, str]) -> Tuple[str, str]:
    matricula = (raw.get("NumeroMatricula") or "").strip()
    due_date = parse_date(raw.get("DataVencimento") or "")
    aluno = normalize_label(raw.get("Sacado") or "")
    cpf = only_digits(raw.get("CPFResponsavel") or "")

    if matricula and due_date:
        key = f"{matricula}|{due_date}"
    elif cpf and aluno and due_date:
        key = f"{cpf}|{aluno}|{due_date}"
    else:
        key = f"{aluno}|{due_date}|{raw.get('ContaReceberID') or ''}"

    return f"sponte-xml-{key}", key


def parse_xml_records(xml_path: str) -> Tuple[List[ChargeRecord], Dict[str, Any]]:
    root = ET.parse(xml_path).getroot()
    rows = root.findall("Table")
    records: List[ChargeRecord] = []
    ignored_count = 0

    for row in rows:
        raw = {child.tag: (child.text or "").strip() for child in row}
        situacao = raw.get("Situacao", "")
        forma = raw.get("FormaCobranca", "")
        if normalize_label(situacao) != "pendente" or normalize_label(forma) != "boleto automatizado":
            ignored_count += 1
            continue

        valor = parse_money(raw.get("Valor", ""))
        valor_liquido = parse_money(raw.get("ValorLiquido", ""))
        valor_com_desconto = parse_money(raw.get("ValorComDesconto", ""))
        valor_final = choose_value(valor_liquido, valor_com_desconto, valor)
        desconto_percentual, desconto_confiavel = infer_discount_percent(valor, valor_final, raw.get("Bolsa", ""))
        payer_source, payer_name, payer_cpf, payer_email, payer_phone = choose_payer(raw)
        external_reference, source_key = build_external_reference(raw)

        blocking_reasons: List[str] = []
        if not payer_name:
            blocking_reasons.append("pagador sem nome")
        if not payer_cpf:
            blocking_reasons.append("pagador sem CPF")
        if not payer_email:
            blocking_reasons.append("pagador sem email")
        if not payer_phone:
            blocking_reasons.append("pagador sem telefone")
        if not parse_date(raw.get("DataVencimento", "")):
            blocking_reasons.append("sem vencimento")
        if valor_final <= 0:
            blocking_reasons.append("valor invalido")

        records.append(
            ChargeRecord(
                external_reference=external_reference,
                source_key=source_key,
                conta_receber_id=raw.get("ContaReceberID", ""),
                numero_matricula=raw.get("NumeroMatricula", ""),
                numero_parcela=raw.get("NumeroParcela", ""),
                aluno=raw.get("Sacado", ""),
                responsavel=raw.get("NomeResponsavel", ""),
                payer_source=payer_source,
                payer_name=payer_name,
                payer_cpf_cnpj=payer_cpf,
                payer_email=payer_email,
                payer_phone=payer_phone,
                due_date=parse_date(raw.get("DataVencimento", "")),
                categoria=raw.get("Categoria", ""),
                turma=raw.get("Turma", ""),
                bolsa_label=raw.get("Bolsa", ""),
                desconto_percentual=desconto_percentual,
                desconto_confiavel=desconto_confiavel,
                valor_cheio=valor,
                valor_final=valor_final,
                valor_original_xml=valor,
                valor_liquido_xml=valor_liquido,
                valor_com_desconto_xml=valor_com_desconto,
                forma_cobranca=forma,
                situacao=situacao,
                ready_to_sync=not blocking_reasons,
                blocking_reason=", ".join(blocking_reasons),
                raw=raw,
            )
        )

    records.sort(key=lambda item: (item.due_date, normalize_label(item.aluno), item.numero_parcela))

    summary = {
        "generated_at": now_iso(),
        "xml_record_count": len(rows),
        "filtered_record_count": len(records),
        "ignored_record_count": ignored_count,
        "ready_to_sync_count": sum(1 for item in records if item.ready_to_sync),
        "blocked_count": sum(1 for item in records if not item.ready_to_sync),
        "discount_reliable_count": sum(1 for item in records if item.desconto_confiavel),
        "discount_fallback_count": sum(1 for item in records if not item.desconto_confiavel),
        "total_full_value": round(sum(item.valor_cheio for item in records), 2),
        "total_final_value": round(sum(item.valor_final for item in records), 2),
    }
    return records, summary


class AsaasClient:
    def __init__(self, access_token: str, base_url: str):
        self.access_token = access_token.strip()
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.verify = CA_BUNDLE_PATH

    def _headers(self) -> Dict[str, str]:
        return {
            "accept": "application/json",
            "content-type": "application/json",
            "access_token": self.access_token,
        }

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        response = self.session.get(
            f"{self.base_url}{path}",
            headers=self._headers(),
            params=params or {},
            timeout=60,
        )
        response.raise_for_status()
        return response.json()

    def post(self, path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        response = self.session.post(
            f"{self.base_url}{path}",
            headers=self._headers(),
            json=payload or {},
            timeout=60,
        )
        response.raise_for_status()
        return response.json()

    def put(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        response = self.session.put(
            f"{self.base_url}{path}",
            headers=self._headers(),
            json=payload,
            timeout=60,
        )
        response.raise_for_status()
        return response.json()

    def delete(self, path: str) -> Dict[str, Any]:
        response = self.session.delete(
            f"{self.base_url}{path}",
            headers=self._headers(),
            timeout=60,
        )
        response.raise_for_status()
        if response.text.strip():
            return response.json()
        return {"deleted": True}

    def find_customer_by_cpf_cnpj(self, cpf_cnpj: str) -> Optional[Dict[str, Any]]:
        if not cpf_cnpj:
            return None
        data = self.get("/customers", params={"cpfCnpj": cpf_cnpj})
        items = data.get("data") or []
        return items[0] if items else None

    def create_customer(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.post("/customers", payload)

    def update_customer(self, customer_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.put(f"/customers/{customer_id}", payload)

    def find_payment_by_external_reference(self, external_reference: str) -> Optional[Dict[str, Any]]:
        data = self.get("/payments", params={"externalReference": external_reference, "limit": 1})
        items = data.get("data") or []
        return items[0] if items else None

    def create_payment(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.post("/payments", payload)

    def update_payment(self, payment_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.put(f"/payments/{payment_id}", payload)

    def delete_payment(self, payment_id: str) -> Dict[str, Any]:
        return self.delete(f"/payments/{payment_id}")

    def delete_customer(self, customer_id: str) -> Dict[str, Any]:
        return self.delete(f"/customers/{customer_id}")


def build_customer_payload(record: ChargeRecord) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "name": record.payer_name,
        "notificationDisabled": True,
    }
    if record.payer_cpf_cnpj:
        payload["cpfCnpj"] = record.payer_cpf_cnpj
    if record.payer_email:
        payload["email"] = record.payer_email
    if record.payer_phone:
        payload["mobilePhone"] = record.payer_phone
    return payload


def build_payment_payload(record: ChargeRecord, customer_id: str) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "customer": customer_id,
        "billingType": "BOLETO",
        "value": record.valor_cheio if record.desconto_confiavel else record.valor_final,
        "dueDate": record.due_date,
        "description": f"{record.aluno} - Parcela {record.numero_parcela or '?'} - {record.categoria or 'Mensalidade'}"[:500],
        "externalReference": record.external_reference,
    }
    if record.desconto_confiavel and record.desconto_percentual:
        payload["discount"] = {
            "value": record.desconto_percentual,
            "dueDateLimitDays": 0,
            "type": "PERCENTAGE",
        }
    return payload


def ensure_customer(client: AsaasClient, record: ChargeRecord) -> Dict[str, Any]:
    payload = build_customer_payload(record)
    existing = client.find_customer_by_cpf_cnpj(record.payer_cpf_cnpj)
    if existing:
        return client.update_customer(existing["id"], payload)
    return client.create_customer(payload)


def sync_record_to_asaas(client: AsaasClient, record: ChargeRecord, state: Dict[str, Any]) -> Dict[str, Any]:
    customer = ensure_customer(client, record)
    payment_payload = build_payment_payload(record, customer["id"])
    existing = client.find_payment_by_external_reference(record.external_reference)

    if existing:
        result = client.update_payment(existing["id"], payment_payload)
        action = "updated"
        payment_id = existing["id"]
    else:
        result = client.create_payment(payment_payload)
        action = "created"
        payment_id = result.get("id")

    saved_status = result.get("status")
    if not saved_status and existing:
        saved_status = existing.get("status")

    state["payments_by_reference"][record.external_reference] = {
        "external_reference": record.external_reference,
        "source_key": record.source_key,
        "payment_id": payment_id,
        "customer_id": customer.get("id"),
        "aluno": record.aluno,
        "responsavel": record.responsavel,
        "due_date": record.due_date,
        "valor_final": record.valor_final,
        "valor_cheio": record.valor_cheio,
        "discount_percent": record.desconto_percentual,
        "discount_reliable": record.desconto_confiavel,
        "status": saved_status,
        "last_action": action,
        "updated_at": now_iso(),
    }

    return {
        "action": action,
        "payment_id": payment_id,
        "customer_id": customer.get("id"),
        "payload": payment_payload,
        "response": result,
    }


def cancel_missing_payment(
    client: AsaasClient,
    external_reference: str,
    state: Dict[str, Any],
    missing_action: str = "school_paid",
) -> Dict[str, Any]:
    item = state.get("payments_by_reference", {}).get(external_reference)
    if not item:
        raise ValueError("Registro nao encontrado no estado local.")
    payment_id = item.get("payment_id")
    if not payment_id:
        raise ValueError("Registro sem payment_id salvo.")

    action = "cancelled" if missing_action == "cancelled" else "school_paid"
    payment_result = item.get("delete_response")
    if not item.get("payment_deleted_at"):
        payment_result = client.delete_payment(payment_id)
        item["payment_deleted_at"] = now_iso()
        item["delete_response"] = payment_result

    customer_id = item.get("customer_id")
    customer_result: Optional[Dict[str, Any]] = None
    if action == "cancelled":
        if customer_id:
            customer_result = item.get("customer_delete_response")
            if not item.get("customer_deleted_at"):
                customer_result = client.delete_customer(customer_id)
                item["customer_deleted_at"] = now_iso()
                item["customer_delete_response"] = customer_result
        else:
            item["customer_delete_skipped"] = "Registro sem customer_id salvo."

    item["last_action"] = "deleted"
    item["missing_action"] = action
    item["updated_at"] = now_iso()
    return {
        "external_reference": external_reference,
        "payment_id": payment_id,
        "customer_id": customer_id or "",
        "action": "Cancelou" if action == "cancelled" else "Pagou na escola",
        "payment_response": payment_result,
        "customer_response": customer_result,
        "customer_skipped": item.get("customer_delete_skipped", ""),
    }


def current_missing_candidates(snapshot_missing: List[Dict[str, Any]], state: Dict[str, Any]) -> List[Dict[str, Any]]:
    missing: List[Dict[str, Any]] = []
    for missing_item in snapshot_missing:
        external_reference = missing_item.get("external_reference") or ""
        if not external_reference:
            continue
        item = state.get("payments_by_reference", {}).get(external_reference)
        if not item:
            continue
        if item.get("last_action") == "deleted":
            continue
        display_item = dict(item)
        display_item.setdefault("external_reference", external_reference)
        display_item.setdefault("aluno", missing_item.get("aluno", ""))
        display_item.setdefault("numero_matricula", missing_item.get("numero_matricula", ""))
        display_item.setdefault("due_date", missing_item.get("due_date", ""))
        missing.append(display_item)
    missing.sort(key=lambda item: (item.get("due_date") or "", normalize_label(item.get("aluno") or "")))
    return missing


def get_sync_info(record: ChargeRecord, state: Dict[str, Any]) -> Dict[str, Any]:
    saved = state.get("payments_by_reference", {}).get(record.external_reference)
    if not saved:
        return {
            "label": "Novo",
            "class_name": "ok",
            "detail": "Ainda nao sincronizado",
            "already_sent": False,
        }

    last_action = saved.get("last_action") or "synced"
    payment_id = saved.get("payment_id") or "-"
    if last_action == "deleted":
        return {
            "label": "Removido",
            "class_name": "warn",
            "detail": f"Payment {payment_id} removido anteriormente",
            "already_sent": False,
        }

    action_label = "Ja enviado"
    action_detail = f"Payment {payment_id} | ultima acao: {last_action}"
    return {
        "label": action_label,
        "class_name": "warn",
        "detail": action_detail,
        "already_sent": True,
    }


def filter_duplicate_records(records: List[ChargeRecord], state: Dict[str, Any]) -> Tuple[List[ChargeRecord], List[ChargeRecord]]:
    allowed: List[ChargeRecord] = []
    duplicates: List[ChargeRecord] = []
    for record in records:
        sync_info = get_sync_info(record, state)
        if sync_info["already_sent"]:
            duplicates.append(record)
        else:
            allowed.append(record)
    return allowed, duplicates


def inspect_records_with_asaas(
    records: List[ChargeRecord],
    state: Dict[str, Any],
    client: Optional["AsaasClient"],
) -> List[Dict[str, Any]]:
    inspected: List[Dict[str, Any]] = []
    for record in records:
        sync_info = get_sync_info(record, state)
        asaas_exists = None
        asaas_payment_id = ""
        if client:
            existing = client.find_payment_by_external_reference(record.external_reference)
            if existing:
                asaas_exists = True
                asaas_payment_id = existing.get("id") or ""
            else:
                asaas_exists = False

        suggested_action = "Criar"
        detail = "pronto para criacao"
        if sync_info["already_sent"]:
            suggested_action = "Bloqueado localmente"
            detail = sync_info["detail"]
        elif asaas_exists:
            suggested_action = "Ja existe no Asaas"
            detail = f"payment {asaas_payment_id}" if asaas_payment_id else "parcela ja encontrada no Asaas"

        inspected.append(
            {
                "record": record,
                "aluno": record.aluno,
                "numero_matricula": record.numero_matricula,
                "due_date": record.due_date,
                "valor_final": record.valor_final,
                "external_reference": record.external_reference,
                "local_already_sent": sync_info["already_sent"],
                "asaas_exists": asaas_exists,
                "asaas_payment_id": asaas_payment_id,
                "suggested_action": suggested_action,
                "detail": detail,
            }
        )
    return inspected


def build_period_key(records: List[ChargeRecord]) -> str:
    months = sorted({item.due_date[:7] for item in records if item.due_date and len(item.due_date) >= 7})
    if not months:
        return "sem-periodo"
    if len(months) == 1:
        return months[0]
    return "multi:" + ",".join(months)


def build_xml_snapshot(records: List[ChargeRecord], xml_path: str) -> Dict[str, Any]:
    return {
        "period_key": build_period_key(records),
        "xml_path": xml_path,
        "generated_at": now_iso(),
        "records_by_reference": {
            item.external_reference: {
                "aluno": item.aluno,
                "numero_matricula": item.numero_matricula,
                "due_date": item.due_date,
            }
            for item in records
        },
    }


def update_snapshot_and_collect_changes(records: List[ChargeRecord], xml_path: str) -> Dict[str, List[Dict[str, Any]]]:
    state = load_state()
    snapshot = build_xml_snapshot(records, xml_path)
    period_key = snapshot["period_key"]
    previous_snapshot = state.get("xml_snapshots_by_period", {}).get(period_key) or {}
    previous_items = previous_snapshot.get("records_by_reference", {})
    current_items = snapshot.get("records_by_reference", {})

    missing_items = []
    added_items = []
    if previous_items:
        for reference, item in previous_items.items():
            if reference not in current_items:
                missing_items.append(
                    {
                        "external_reference": reference,
                        "aluno": item.get("aluno", ""),
                        "numero_matricula": item.get("numero_matricula", ""),
                        "due_date": item.get("due_date", ""),
                    }
                )
        for reference, item in current_items.items():
            if reference not in previous_items:
                added_items.append(
                    {
                        "external_reference": reference,
                        "aluno": item.get("aluno", ""),
                        "numero_matricula": item.get("numero_matricula", ""),
                        "due_date": item.get("due_date", ""),
                    }
                )

    missing_items.sort(key=lambda item: (item.get("due_date") or "", normalize_label(item.get("aluno") or "")))
    added_items.sort(key=lambda item: (item.get("due_date") or "", normalize_label(item.get("aluno") or "")))
    state["xml_snapshots_by_period"][period_key] = snapshot
    save_state(state)
    APP_STATE["snapshot_missing"] = missing_items
    APP_STATE["snapshot_added"] = added_items
    return {"missing": missing_items, "added": added_items}


def update_snapshot_and_collect_missing(records: List[ChargeRecord], xml_path: str) -> List[Dict[str, Any]]:
    return update_snapshot_and_collect_changes(records, xml_path)["missing"]


def start_sync_job(
    selected_records: List[ChargeRecord],
    asaas_base_url: str,
    asaas_access_token: str,
) -> str:
    job_id = uuid.uuid4().hex[:12]
    with SYNC_JOBS_LOCK:
        SYNC_JOBS[job_id] = {
            "job_id": job_id,
            "status": "running",
            "created_at": now_iso(),
            "started_at": now_iso(),
            "finished_at": "",
            "total": len(selected_records),
            "processed": 0,
            "success_count": 0,
            "error_count": 0,
            "current_label": "",
            "results": [],
            "errors": [],
        }

    worker = threading.Thread(
        target=run_sync_job,
        args=(job_id, selected_records, asaas_base_url, asaas_access_token),
        daemon=True,
    )
    worker.start()
    return job_id


def run_sync_job(
    job_id: str,
    selected_records: List[ChargeRecord],
    asaas_base_url: str,
    asaas_access_token: str,
) -> None:
    logger.info("Job iniciado | job_id=%s | total=%s", job_id, len(selected_records))
    client = AsaasClient(asaas_access_token, asaas_base_url)
    state = load_state()

    try:
        for index, record in enumerate(selected_records, start=1):
            with SYNC_JOBS_LOCK:
                job = SYNC_JOBS[job_id]
                job["current_label"] = f"{record.aluno} ({index}/{job['total']})"

            try:
                result = sync_record_to_asaas(client, record, state)
                result["title"] = record.aluno
                logger.info(
                    "Sincronizacao ok | job_id=%s | aluno=%s | external_reference=%s | action=%s | payment_id=%s",
                    job_id,
                    record.aluno,
                    record.external_reference,
                    result.get("action"),
                    result.get("payment_id"),
                )
                with SYNC_JOBS_LOCK:
                    job = SYNC_JOBS[job_id]
                    job["results"].append(result)
                    job["success_count"] += 1
                    job["processed"] = index
            except Exception as exc:
                logger.exception(
                    "Erro ao sincronizar | job_id=%s | aluno=%s | external_reference=%s",
                    job_id,
                    record.aluno,
                    record.external_reference,
                )
                with SYNC_JOBS_LOCK:
                    job = SYNC_JOBS[job_id]
                    job["errors"].append(f"{record.aluno}: {exc}")
                    job["error_count"] += 1
                    job["processed"] = index

        save_state(state)
        with SYNC_JOBS_LOCK:
            job = SYNC_JOBS[job_id]
            job["status"] = "completed"
            job["finished_at"] = now_iso()
            job["current_label"] = "Concluido"
        logger.info("Job concluido | job_id=%s", job_id)
    except Exception:
        logger.exception("Falha geral no job | job_id=%s", job_id)
        with SYNC_JOBS_LOCK:
            job = SYNC_JOBS[job_id]
            job["status"] = "failed"
            job["finished_at"] = now_iso()
            if not job["errors"]:
                job["errors"].append("Falha inesperada no processamento do lote.")


def load_config() -> Dict[str, str]:
    config = {
        "asaas_base_url": DEFAULT_ASAAS_BASE_URL,
        "asaas_access_token": "",
    }
    if not os.path.exists(CONFIG_FILE):
        return config
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        if isinstance(data, dict):
            config["asaas_base_url"] = str(data.get("asaas_base_url") or DEFAULT_ASAAS_BASE_URL)
            config["asaas_access_token"] = str(data.get("asaas_access_token") or "")
    except Exception:
        logger.exception("Falha ao carregar configuracao local.")
    return config


def save_config(base_url: str, access_token: str) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as file:
        json.dump(
            {
                "asaas_base_url": base_url,
                "asaas_access_token": access_token,
                "updated_at": now_iso(),
            },
            file,
            ensure_ascii=False,
            indent=2,
        )
    logger.info("Configuracao local salva.")


def replace_records_with_overrides(handler: BaseHTTPRequestHandler, selected_refs: List[str]) -> List[ChargeRecord]:
    records_by_reference = APP_STATE.get("records_by_reference", {})
    updated: List[ChargeRecord] = []

    for reference in selected_refs:
        original: ChargeRecord = records_by_reference[reference]
        payer_name = get_form_value(handler, f"payer_name::{reference}") or original.payer_name
        payer_cpf = only_digits(get_form_value(handler, f"payer_cpf::{reference}") or original.payer_cpf_cnpj)
        payer_email = (get_form_value(handler, f"payer_email::{reference}") or original.payer_email).strip()
        payer_phone = only_digits(get_form_value(handler, f"payer_phone::{reference}") or original.payer_phone)
        updated_record = replace(
            original,
            payer_name=payer_name.strip(),
            payer_cpf_cnpj=payer_cpf,
            payer_email=payer_email,
            payer_phone=payer_phone,
        )
        blocking = []
        if not updated_record.payer_name:
            blocking.append("pagador sem nome")
        if not updated_record.payer_cpf_cnpj:
            blocking.append("pagador sem CPF")
        if not updated_record.payer_email:
            blocking.append("pagador sem email")
        if not updated_record.payer_phone:
            blocking.append("pagador sem telefone")
        updated_record = replace(
            updated_record,
            ready_to_sync=not blocking,
            blocking_reason=", ".join(blocking),
        )
        updated.append(updated_record)
    return updated


def get_form_values(handler: BaseHTTPRequestHandler) -> Dict[str, List[str]]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    body = handler.rfile.read(length).decode("utf-8")
    return parse_qs(body, keep_blank_values=True)


def get_form_value(handler: BaseHTTPRequestHandler, key: str, default: str = "") -> str:
    if not hasattr(handler, "_form_data"):
        handler._form_data = get_form_values(handler)
    return handler._form_data.get(key, [default])[0]


def get_form_list(handler: BaseHTTPRequestHandler, key: str) -> List[str]:
    if not hasattr(handler, "_form_data"):
        handler._form_data = get_form_values(handler)
    return handler._form_data.get(key, [])


def parse_multipart(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    content_type = handler.headers.get("Content-Type", "")
    if "multipart/form-data" not in content_type:
        return {"fields": {}, "files": {}}

    length = int(handler.headers.get("Content-Length", "0") or "0")
    raw_body = handler.rfile.read(length)
    mime_bytes = (
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
        + raw_body
    )
    message = BytesParser(policy=default).parsebytes(mime_bytes)

    fields: Dict[str, List[str]] = {}
    files: Dict[str, Dict[str, Any]] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            files[name] = {"filename": filename, "content": payload}
        else:
            fields.setdefault(name, []).append(payload.decode("utf-8", errors="replace"))
    return {"fields": fields, "files": files}


def save_uploaded_xml(upload: Dict[str, Any]) -> str:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    original_name = os.path.basename(upload.get("filename") or "sponte.xml")
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", original_name)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target_path = os.path.join(UPLOAD_DIR, f"{timestamp}-{safe_name}")
    with open(target_path, "wb") as file:
        file.write(upload.get("content") or b"")
    return target_path


def html_page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{
      --bg: #f4efe6;
      --paper: #fffdf8;
      --ink: #1d2a30;
      --muted: #61727c;
      --line: #d8cec2;
      --brand: #0e7490;
      --brand-2: #d97706;
      --ok: #166534;
      --warn: #b45309;
      --bad: #b91c1c;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      font-family: Georgia, "Times New Roman", serif;
      background:
        radial-gradient(circle at top left, rgba(14,116,144,.13), transparent 28%),
        radial-gradient(circle at top right, rgba(217,119,6,.10), transparent 30%),
        linear-gradient(180deg, #faf6ef 0%, var(--bg) 100%);
    }}
    .shell {{
      max-width: 1380px;
      margin: 0 auto;
      padding: 28px 20px 40px;
    }}
    .hero {{
      background: linear-gradient(135deg, rgba(255,253,248,.92), rgba(245,238,228,.95));
      border: 1px solid rgba(29,42,48,.08);
      border-radius: 24px;
      padding: 28px;
      box-shadow: 0 22px 60px rgba(41, 33, 21, .08);
    }}
    .hero h1 {{
      margin: 0 0 10px;
      font-size: clamp(28px, 4vw, 46px);
      line-height: 1;
      letter-spacing: -.03em;
    }}
    .hero p {{
      margin: 0;
      max-width: 900px;
      color: var(--muted);
      font-size: 16px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      margin: 20px 0;
    }}
    .card {{
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 16px;
      box-shadow: 0 10px 24px rgba(0, 0, 0, .04);
    }}
    .kpi {{
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .08em;
      color: var(--muted);
      margin-bottom: 8px;
    }}
    .kpi-value {{
      font-size: 28px;
      font-weight: bold;
    }}
    .kpi-sub {{
      margin-top: 6px;
      color: var(--muted);
      font-size: 14px;
    }}
    form {{
      margin: 0;
    }}
    .toolbar {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      align-items: end;
      margin: 18px 0 8px;
    }}
    label {{
      display: block;
      font-size: 13px;
      color: var(--muted);
      margin-bottom: 5px;
    }}
    input[type="text"], input[type="password"], input[type="search"], input[type="email"], textarea, select {{
      width: 100%;
      padding: 12px 14px;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: white;
      color: var(--ink);
      font-size: 14px;
    }}
    .btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 44px;
      padding: 0 18px;
      border-radius: 999px;
      border: 0;
      cursor: pointer;
      text-decoration: none;
      font-weight: bold;
      color: white;
      background: var(--brand);
    }}
    .btn.secondary {{
      background: #39586a;
    }}
    .btn.warn {{
      background: var(--brand-2);
    }}
    .btn.danger {{
      background: var(--bad);
    }}
    .message {{
      padding: 12px 14px;
      border-radius: 14px;
      margin: 14px 0;
      border: 1px solid var(--line);
      background: #fff;
    }}
    .message.error {{
      border-color: rgba(185,28,28,.25);
      background: rgba(185,28,28,.06);
    }}
    .message.success {{
      border-color: rgba(22,101,52,.25);
      background: rgba(22,101,52,.07);
    }}
    .message.info {{
      border-color: rgba(14,116,144,.25);
      background: rgba(14,116,144,.06);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: rgba(255,255,255,.86);
      border: 1px solid var(--line);
      border-radius: 18px;
      overflow: hidden;
    }}
    th, td {{
      padding: 12px 10px;
      border-bottom: 1px solid #ece4d8;
      vertical-align: top;
      font-size: 14px;
    }}
    th {{
      text-align: left;
      background: #f8f2e8;
      position: sticky;
      top: 0;
      z-index: 1;
    }}
    tr:hover td {{
      background: rgba(14,116,144,.03);
    }}
    .pill {{
      display: inline-block;
      padding: 4px 9px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: bold;
      white-space: nowrap;
    }}
    .pill.ok {{ color: var(--ok); background: rgba(22,101,52,.10); }}
    .pill.warn {{ color: var(--warn); background: rgba(180,83,9,.10); }}
    .pill.bad {{ color: var(--bad); background: rgba(185,28,28,.10); }}
    .muted {{ color: var(--muted); }}
    .stack > * + * {{ margin-top: 12px; }}
    .two-col {{
      display: grid;
      grid-template-columns: 1.2fr .8fr;
      gap: 18px;
      align-items: start;
    }}
    .record-box {{
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 16px;
      background: rgba(255,255,255,.86);
    }}
    .record-box h3 {{
      margin: 0 0 10px;
      font-size: 19px;
    }}
    .mono {{
      font-family: "Courier New", monospace;
      font-size: 12px;
      background: #f7f3ec;
      border: 1px solid #eadfcf;
      border-radius: 12px;
      padding: 12px;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    .input-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}
    .section-title {{
      margin: 22px 0 10px;
      font-size: 22px;
    }}
    .small {{
      font-size: 12px;
    }}
    .mini-grid {{
      display:grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap:10px;
    }}
    .mini-box {{
      border:1px solid #ece4d8;
      border-radius:12px;
      padding:10px 12px;
      background:#fff;
    }}
    .mini-box strong {{
      display:block;
      font-size:12px;
      color:var(--muted);
      margin-bottom:4px;
      text-transform:uppercase;
      letter-spacing:.06em;
    }}
    .compact-table th, .compact-table td {{
      padding:10px 8px;
      font-size:13px;
    }}
    .row-create td {{
      background: rgba(22,101,52,.03);
    }}
    .row-existing td {{
      background: rgba(180,83,9,.05);
    }}
    .row-blocked td {{
      background: rgba(185,28,28,.04);
    }}
    @media (max-width: 1000px) {{
      .grid, .two-col, .input-grid, .mini-grid {{
        grid-template-columns: 1fr;
      }}
      table {{
        display: block;
        overflow-x: auto;
      }}
    }}
  </style>
</head>
<body>
  <div class="shell">{body}</div>
  <script>
    const search = document.getElementById('table-filter');
    const syncFilter = document.getElementById('sync-filter');
    function applyTableFilters() {{
      const needle = search ? search.value.toLowerCase() : '';
      const syncMode = syncFilter ? syncFilter.value : 'all';
      document.querySelectorAll('[data-row-search]').forEach((row) => {{
        const checkbox = row.querySelector('input[name="selected_ref"]');
        const syncState = checkbox ? checkbox.dataset.syncState : 'new';
        const matchesText = row.dataset.rowSearch.includes(needle);
        const matchesSync = syncMode === 'all' || syncState === syncMode;
        row.style.display = matchesText && matchesSync ? '' : 'none';
      }});
    }}
    if (search) {{
      search.addEventListener('input', applyTableFilters);
    }}
    if (syncFilter) {{
      syncFilter.addEventListener('change', applyTableFilters);
    }}
    const toggleAll = document.getElementById('toggle-all');
    if (toggleAll) {{
      toggleAll.addEventListener('change', () => {{
        document.querySelectorAll('input[name="selected_ref"]').forEach((box) => {{
          if (!box.disabled) box.checked = toggleAll.checked;
        }});
      }});
    }}
    const toggleMissing = document.getElementById('toggle-missing');
    if (toggleMissing) {{
      toggleMissing.addEventListener('change', () => {{
        document.querySelectorAll('input[name="missing_ref"]').forEach((box) => {{
          box.checked = toggleMissing.checked;
        }});
      }});
    }}
  </script>
</body>
</html>"""


def render_messages() -> str:
    blocks = []
    for message in pop_messages():
        blocks.append(f'<div class="message {escape(message["kind"])}">{escape(message["text"])}</div>')
    return "".join(blocks)


def render_home() -> str:
    records: List[ChargeRecord] = APP_STATE.get("records", [])
    summary = APP_STATE.get("summary", {})
    state = load_state()
    config = load_config()
    snapshot_missing = APP_STATE.get("snapshot_missing") or []
    snapshot_added = APP_STATE.get("snapshot_added") or []
    missing_items = current_missing_candidates(snapshot_missing, state)
    already_sent_count = sum(1 for item in records if get_sync_info(item, state)["already_sent"])
    new_count = sum(1 for item in records if not get_sync_info(item, state)["already_sent"])
    sendable_count = sum(1 for item in records if item.ready_to_sync and not get_sync_info(item, state)["already_sent"])

    if records:
        cards = f"""
        <div class="grid">
          <div class="card"><div class="kpi">Parcelas elegiveis</div><div class="kpi-value">{summary.get("filtered_record_count", 0)}</div><div class="kpi-sub">apos filtro Pendente + Boleto Automatizado</div></div>
          <div class="card"><div class="kpi">Prontas para envio</div><div class="kpi-value">{sendable_count}</div><div class="kpi-sub">novas e sem campos obrigatorios faltando</div></div>
          <div class="card"><div class="kpi">Valor final</div><div class="kpi-value">{escape(format_currency(summary.get("total_final_value", 0.0)))}</div><div class="kpi-sub">somatorio usando prioridade ValorLiquido / ValorComDesconto / Valor</div></div>
          <div class="card"><div class="kpi">Ja enviados</div><div class="kpi-value">{already_sent_count}</div><div class="kpi-sub">{new_count} registro(s) novos nesta leitura</div></div>
        </div>
        """
    else:
        cards = ""

    comparison_section = ""
    if snapshot_added or snapshot_missing:
        added_preview = "".join(
            f"<li>{escape(item.get('aluno') or '-')}" + (f" - matricula {escape(item.get('numero_matricula') or '-')}" if item.get("numero_matricula") else "") + "</li>"
            for item in snapshot_added[:12]
        )
        missing_preview = "".join(
            f"<li>{escape(item.get('aluno') or '-')}" + (f" - matricula {escape(item.get('numero_matricula') or '-')}" if item.get("numero_matricula") else "") + "</li>"
            for item in snapshot_missing[:12]
        )
        added_more = ""
        missing_more = ""
        if len(snapshot_added) > 12:
            added_more = f"<p class=\"muted small\">E mais {len(snapshot_added) - 12} entrada(s).</p>"
        if len(snapshot_missing) > 12:
            missing_more = f"<p class=\"muted small\">E mais {len(snapshot_missing) - 12} saida(s).</p>"
        added_block = (
            f"""
            <div class="mini-box">
              <strong>Entraram neste XML</strong>
              <div class="kpi-value">{len(snapshot_added)}</div>
              <ul>{added_preview}</ul>
              {added_more}
            </div>
            """
            if snapshot_added
            else '<div class="mini-box"><strong>Entraram neste XML</strong><div class="kpi-value">0</div><p class="muted small">Nenhuma cobranca nova em relacao ao XML anterior.</p></div>'
        )
        missing_block = (
            f"""
            <div class="mini-box">
              <strong>Nao aparecem neste XML</strong>
              <div class="kpi-value">{len(snapshot_missing)}</div>
              <ul>{missing_preview}</ul>
              {missing_more}
            </div>
            """
            if snapshot_missing
            else '<div class="mini-box"><strong>Nao aparecem neste XML</strong><div class="kpi-value">0</div><p class="muted small">Nenhuma cobranca saiu em relacao ao XML anterior.</p></div>'
        )
        comparison_section = f"""
        <div class="card" style="margin-top:22px; border-color: rgba(14,116,144,.22); background: rgba(14,116,144,.04);">
          <h2 class="section-title">Comparacao com o XML anterior</h2>
          <p class="muted">Esta area so compara a ultima leitura do mesmo periodo com o XML carregado agora. Quem entrou aparece na lista principal para envio; as acoes Pagou na escola e Cancelou ficam apenas para cobrancas que nao aparecem mais.</p>
          <div class="mini-grid">
            {added_block}
            {missing_block}
          </div>
        </div>
        """

    rows = []
    for record in records:
        sync_info = get_sync_info(record, state)
        search_blob = normalize_label(
            f"{record.aluno} {record.responsavel} {record.numero_matricula} {record.turma} {record.payer_name}"
        )
        if sync_info["already_sent"]:
            status_class = "warn"
            status_label = "Nao enviar"
            status_detail = "Ja sincronizado anteriormente"
        elif record.ready_to_sync:
            status_class = "ok"
            status_label = "Dados ok"
            status_detail = "Liberado para selecao"
        else:
            status_class = "bad"
            status_label = "Bloqueado"
            status_detail = record.blocking_reason or "Pendencia nos dados"
        discount_text = (
            f"{record.desconto_percentual:.2f}% separado"
            if record.desconto_confiavel and record.desconto_percentual
            else "fallback: valor final"
        )
        rows.append(
            f"""
            <tr data-row-search="{escape(search_blob)}">
              <td>
                <input
                  type="checkbox"
                  name="selected_ref"
                  value="{escape(record.external_reference)}"
                  data-sync-state="{escape('sent' if sync_info['already_sent'] else 'new')}"
                  {'disabled' if (not record.ready_to_sync or sync_info['already_sent']) else ''}
                >
              </td>
              <td>
                <strong>{escape(record.aluno)}</strong><br>
                <span class="muted small">Matricula {escape(record.numero_matricula or '-')} | Parcela {escape(record.numero_parcela or '-')}</span>
              </td>
              <td>{escape(record.responsavel or '-')}</td>
              <td>
                <strong>{escape(record.payer_name or '-')}</strong><br>
                <span class="muted small">{escape(record.payer_source)}</span>
              </td>
              <td>{escape(format_currency(record.valor_final))}</td>
              <td>{escape(format_currency(record.valor_cheio))}</td>
              <td>{escape(record.due_date)}</td>
              <td>{escape(record.bolsa_label or '-')}<br><span class="muted small">{escape(discount_text)}</span></td>
              <td><span class="pill {sync_info['class_name']}">{escape(sync_info['label'])}</span><br><span class="muted small">{escape(sync_info['detail'])}</span></td>
              <td><span class="pill {status_class}">{escape(status_label)}</span><br><span class="muted small">{escape(status_detail)}</span></td>
            </tr>
            """
        )

    missing_section = ""
    if missing_items:
        missing_rows = []
        for item in missing_items:
            external_reference = item.get("external_reference", "")
            missing_rows.append(
                f"""
                <tr>
                  <td><input type="checkbox" name="missing_ref" value="{escape(external_reference)}"></td>
                  <td>{escape(item.get('aluno', '-'))}</td>
                  <td>{escape(item.get('due_date', '-'))}</td>
                  <td>{escape(format_currency(float(item.get('valor_final') or 0.0)))}</td>
                  <td>{escape(item.get('payment_id', '-'))}</td>
                  <td>{escape(item.get('last_action', '-'))}</td>
                  <td>
                    <select name="missing_action::{escape(external_reference)}">
                      <option value="school_paid">Pagou na escola - retirar so a cobranca</option>
                      <option value="cancelled">Cancelou - retirar cobranca e cliente</option>
                    </select>
                  </td>
                </tr>
                """
            )
        missing_section = f"""
        <div class="card" style="margin-top:22px;">
          <h2 class="section-title">Cobrancas que nao aparecem neste XML</h2>
          <p class="muted">Estas cobrancas estavam no XML anterior do mesmo periodo, ja possuem cobranca criada no Asaas e agora nao aparecem no XML carregado. Use esta area apenas para decidir se a cobranca deve ser removida por pagamento na escola ou por cancelamento.</p>
          <form method="post" action="/cancel-missing" class="stack">
            <div><label><input type="checkbox" id="toggle-missing"> Selecionar todas que nao aparecem neste XML</label></div>
            <div style="overflow:auto;">
              <table>
                <thead>
                  <tr>
                    <th></th>
                    <th>Aluno</th>
                    <th>Vencimento</th>
                    <th>Valor</th>
                    <th>Payment ID</th>
                    <th>Ultima acao</th>
                    <th>O que aconteceu</th>
                  </tr>
                </thead>
                <tbody>{''.join(missing_rows)}</tbody>
              </table>
            </div>
            <div class="toolbar">
              <div style="min-width:280px; flex:1;">
                <label>Base URL do Asaas</label>
                <input type="text" name="asaas_base_url" value="{escape(config.get('asaas_base_url') or DEFAULT_ASAAS_BASE_URL)}">
              </div>
              <div style="min-width:320px; flex:1;">
                <label>Access token do Asaas</label>
                <input type="password" name="asaas_access_token" value="{escape(config.get('asaas_access_token') or '')}">
              </div>
              <button class="btn danger" type="submit">Aplicar nos selecionados</button>
            </div>
          </form>
        </div>
        """

    return html_page(
        "Sponte XML > Asaas",
        f"""
        <div class="hero">
          <h1>Sponte XML para Asaas</h1>
          <p>MVP manual para o financeiro: carregar o XML mensal do Sponte, revisar parcelas, marcar manualmente o que deve seguir para o Asaas e enviar so o que foi selecionado.</p>
          {render_messages()}
          <form method="post" action="/load-xml">
            <div class="toolbar">
              <div style="flex:1; min-width:320px;">
                <label>Caminho local do XML exportado do Sponte</label>
                <input type="text" name="xml_path" value="{escape(APP_STATE.get('xml_path') or DEFAULT_XML_PATH)}">
              </div>
              <button class="btn" type="submit">Carregar XML</button>
              <a class="btn secondary" href="/report">Abrir JSON atual</a>
            </div>
          </form>
          <form method="post" action="/upload-xml" enctype="multipart/form-data" style="margin-top:14px;">
            <div class="toolbar">
              <div style="flex:1; min-width:320px;">
                <label>Anexar XML do mes pela tela</label>
                <input type="file" name="xml_file" accept=".xml,text/xml,application/xml">
              </div>
              <button class="btn warn" type="submit">Anexar e ler XML</button>
            </div>
          </form>
          <form method="post" action="/save-settings" style="margin-top:14px;">
            <div class="toolbar">
              <div style="min-width:260px; flex:1;">
                <label>Base URL do Asaas</label>
                <input type="text" name="asaas_base_url" value="{escape(config.get('asaas_base_url') or DEFAULT_ASAAS_BASE_URL)}">
              </div>
              <div style="min-width:320px; flex:1;">
                <label>Access token do Asaas salvo neste computador</label>
                <input type="password" name="asaas_access_token" value="{escape(config.get('asaas_access_token') or '')}">
              </div>
              <button class="btn secondary" type="submit">Salvar configuracao</button>
              <button class="btn warn" type="submit" formaction="/test-asaas">Testar conexao</button>
            </div>
          </form>
        </div>
        {cards}
        {comparison_section}
        <div class="card" style="margin-top:22px;">
          <div class="toolbar">
            <div style="flex:1; min-width:260px;">
              <label>Filtrar tabela</label>
              <input id="table-filter" type="search" placeholder="Aluno, responsavel, matricula, turma...">
            </div>
            <div style="min-width:220px;">
              <label>Filtro rapido</label>
              <select id="sync-filter" style="width:100%; padding:12px 14px; border-radius:12px; border:1px solid var(--line); background:white; color:var(--ink); font-size:14px;">
                <option value="all">Todos</option>
                <option value="new">So novos</option>
                <option value="sent">So ja enviados</option>
              </select>
            </div>
          </div>
          <form method="post" action="/review-selected" class="stack">
            <div><label><input type="checkbox" id="toggle-all"> Selecionar todos os registros novos e prontos</label></div>
            <div style="overflow:auto;">
              <table>
                <thead>
                  <tr>
                    <th></th>
                    <th>Aluno</th>
                    <th>Responsavel</th>
                    <th>Pagador</th>
                    <th>Valor final</th>
                    <th>Valor cheio</th>
                    <th>Vencimento</th>
                    <th>Bolsa / desconto</th>
                    <th>Sincronizacao</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>{''.join(rows) if rows else '<tr><td colspan="10" class="muted">Carregue um XML para listar as parcelas.</td></tr>'}</tbody>
              </table>
            </div>
            <button class="btn warn" type="submit">Revisar selecionados antes de enviar</button>
          </form>
        </div>
        {missing_section}
        """,
    )


def render_review(selected_records: List[ChargeRecord], asaas_base_url: str, asaas_access_token: str = "") -> str:
    payload_boxes = []
    for record in selected_records:
        preview_payload = build_payment_payload(record, "cus_preview")
        badge = "ok" if record.ready_to_sync else "bad"
        payload_boxes.append(
            f"""
            <div class="record-box stack">
              <h3>{escape(record.aluno)}</h3>
              <div><span class="pill {badge}">{escape('Pronto para envio' if record.ready_to_sync else 'Bloqueado')}</span></div>
              <div class="input-grid">
                <div>
                  <label>Pagador</label>
                  <input type="text" name="payer_name::{escape(record.external_reference)}" value="{escape(record.payer_name)}">
                </div>
                <div>
                  <label>CPF</label>
                  <input type="text" name="payer_cpf::{escape(record.external_reference)}" value="{escape(record.payer_cpf_cnpj)}">
                </div>
                <div>
                  <label>Email</label>
                  <input type="email" name="payer_email::{escape(record.external_reference)}" value="{escape(record.payer_email)}">
                </div>
                <div>
                  <label>Celular</label>
                  <input type="text" name="payer_phone::{escape(record.external_reference)}" value="{escape(record.payer_phone)}">
                </div>
              </div>
              <div class="muted">
                Matrícula {escape(record.numero_matricula or '-')} | Vencimento {escape(record.due_date)} | Valor final {escape(format_currency(record.valor_final))}
              </div>
              <div class="muted">
                Desconto: {escape(f'{record.desconto_percentual:.2f}% separado' if record.desconto_confiavel and record.desconto_percentual else 'sem percentual confiavel, vai so valor final')}
              </div>
              <div class="mono">{escape(json.dumps(preview_payload, ensure_ascii=False, indent=2))}</div>
              <input type="hidden" name="selected_ref" value="{escape(record.external_reference)}">
            </div>
            """
        )

    return html_page(
        "Revisao antes do envio",
        f"""
        <div class="hero">
          <h1>Revisao final antes do Asaas</h1>
          <p>Esta etapa existe para o financeiro validar ou corrigir os dados do pagador. Se algum aluno estiver sem CPF, email ou telefone no XML, voce pode completar aqui antes do envio.</p>
          {render_messages()}
        </div>
        <form method="post" action="/sync-selected" class="stack" style="margin-top:20px;">
          <div class="card">
            <div class="toolbar">
              <div style="min-width:260px; flex:1;">
                <label>Base URL do Asaas</label>
                <input type="text" name="asaas_base_url" value="{escape(asaas_base_url or DEFAULT_ASAAS_BASE_URL)}">
              </div>
              <div style="min-width:300px; flex:1;">
                <label>Access token do Asaas</label>
                <input type="password" name="asaas_access_token" value="{escape(asaas_access_token)}">
              </div>
            </div>
          </div>
          <div class="two-col">
            <div class="stack">{''.join(payload_boxes)}</div>
            <div class="stack">
              <div class="card">
                <div class="kpi">Selecionados</div>
                <div class="kpi-value">{len(selected_records)}</div>
                <div class="kpi-sub">Apenas esses registros serao enviados.</div>
              </div>
              <div class="card">
                <div class="kpi">Valor final selecionado</div>
                <div class="kpi-value">{escape(format_currency(sum(item.valor_final for item in selected_records)))}</div>
                <div class="kpi-sub">Se o desconto for confiavel, o payload vai com valor cheio + discount percentual.</div>
              </div>
              <div class="card stack">
                <button class="btn" type="submit" name="mode" value="dry-run">Executar dry run</button>
                <button class="btn warn" type="submit" name="mode" value="send">Enviar selecionados ao Asaas</button>
                <a class="btn secondary" href="/">Voltar para a lista</a>
              </div>
            </div>
          </div>
        </form>
        """,
    )


def render_sync_result(title: str, results: List[Dict[str, Any]], errors: List[str]) -> str:
    result_boxes = []
    for item in results:
        result_boxes.append(
            f"""
            <div class="record-box stack">
              <h3>{escape(item.get('title', 'Resultado'))}</h3>
              <div class="mono">{escape(json.dumps(item, ensure_ascii=False, indent=2))}</div>
            </div>
            """
        )
    error_blocks = "".join(f'<div class="message error">{escape(error)}</div>' for error in errors)
    return html_page(
        title,
        f"""
        <div class="hero">
          <h1>{escape(title)}</h1>
          <p>Resultado da operacao executada no fluxo manual do MVP.</p>
          {error_blocks}
        </div>
        <div class="stack" style="margin-top:20px;">
          {''.join(result_boxes) if result_boxes else '<div class="card">Nenhum resultado.</div>'}
          <a class="btn secondary" href="/">Voltar</a>
        </div>
        """,
    )


def render_dry_run_result(items: List[Dict[str, Any]], used_asaas_check: bool) -> str:
    new_count = sum(1 for item in items if item.get("suggested_action") == "Criar")
    existing_count = sum(1 for item in items if item.get("suggested_action") == "Ja existe no Asaas")
    blocked_count = sum(1 for item in items if item.get("suggested_action") == "Bloqueado localmente")

    rows = []
    for item in items:
        badge_class = "ok" if item.get("suggested_action") == "Criar" else "warn"
        rows.append(
            f"""
            <tr>
              <td>{escape(item.get('aluno', '-'))}</td>
              <td>{escape(item.get('numero_matricula', '-'))}</td>
              <td>{escape(item.get('due_date', '-'))}</td>
              <td>{escape(format_currency(float(item.get('valor_final') or 0.0)))}</td>
              <td><span class="pill {'warn' if item.get('local_already_sent') else 'ok'}">{escape('Ja enviado' if item.get('local_already_sent') else 'Novo')}</span></td>
              <td><span class="pill {'warn' if item.get('asaas_exists') else 'ok'}">{escape('Ja existe' if item.get('asaas_exists') else ('Nao consultado' if item.get('asaas_exists') is None else 'Nao existe'))}</span></td>
              <td><span class="pill {badge_class}">{escape(item.get('suggested_action', '-'))}</span></td>
              <td class="muted small">{escape(item.get('detail', '-'))}</td>
            </tr>
            """
        )

    note = (
        "O dry run consultou o Asaas por externalReference para conferir se a parcela ja existe."
        if used_asaas_check
        else "O dry run nao consultou o Asaas porque nao havia token informado. A conferência foi apenas local."
    )
    return html_page(
        "Dry Run",
        f"""
        <div class="hero">
          <h1>Previa do envio</h1>
          <p>{escape(note)}</p>
        </div>
        <div class="grid">
          <div class="card"><div class="kpi">Total analisado</div><div class="kpi-value">{len(items)}</div></div>
          <div class="card"><div class="kpi">Criaria</div><div class="kpi-value">{new_count}</div></div>
          <div class="card"><div class="kpi">Ja existe no Asaas</div><div class="kpi-value">{existing_count}</div></div>
          <div class="card"><div class="kpi">Bloqueado localmente</div><div class="kpi-value">{blocked_count}</div></div>
        </div>
        <div class="card" style="margin-top:20px; overflow:auto;">
          <table>
            <thead>
              <tr>
                <th>Aluno</th>
                <th>Matricula</th>
                <th>Vencimento</th>
                <th>Valor</th>
                <th>Historico local</th>
                <th>Asaas</th>
                <th>Acao sugerida</th>
                <th>Observacao</th>
              </tr>
            </thead>
            <tbody>{''.join(rows)}</tbody>
          </table>
        </div>
        <div style="margin-top:18px;">
          <a class="btn secondary" href="/">Voltar</a>
        </div>
        """,
    )


def render_review(selected_records: List[ChargeRecord], asaas_base_url: str, asaas_access_token: str = "") -> str:
    payload_boxes = []
    for record in selected_records:
        badge = "ok" if record.ready_to_sync else "bad"
        badge_label = "Pronto para envio" if record.ready_to_sync else "Bloqueado"
        discount_text = (
            f"{record.desconto_percentual:.2f}% separado"
            if record.desconto_confiavel and record.desconto_percentual
            else "Sem percentual confiavel: vai so valor final"
        )
        payload_boxes.append(
            f"""
            <div class="record-box stack">
              <div class="toolbar" style="align-items:flex-start; gap:10px;">
                <div style="flex:1; min-width:260px;">
                  <h3 style="margin:0 0 6px;">{escape(record.aluno)}</h3>
                  <div class="muted small">{escape(record.responsavel or 'Sem responsavel')} | pagador {escape(record.payer_source)}</div>
                </div>
                <div><span class="pill {badge}">{escape(badge_label)}</span></div>
              </div>
              <div class="mini-grid">
                <div class="mini-box"><strong>Matricula</strong>{escape(record.numero_matricula or '-')}</div>
                <div class="mini-box"><strong>Parcela</strong>{escape(record.numero_parcela or '-')}</div>
                <div class="mini-box"><strong>Vencimento</strong>{escape(record.due_date or '-')}</div>
                <div class="mini-box"><strong>Valor final</strong>{escape(format_currency(record.valor_final))}</div>
                <div class="mini-box"><strong>Valor cheio</strong>{escape(format_currency(record.valor_cheio))}</div>
                <div class="mini-box"><strong>Desconto</strong>{escape(discount_text)}</div>
              </div>
              <div class="input-grid">
                <div>
                  <label>Pagador</label>
                  <input type="text" name="payer_name::{escape(record.external_reference)}" value="{escape(record.payer_name)}">
                </div>
                <div>
                  <label>CPF</label>
                  <input type="text" name="payer_cpf::{escape(record.external_reference)}" value="{escape(record.payer_cpf_cnpj)}">
                </div>
                <div>
                  <label>Email</label>
                  <input type="email" name="payer_email::{escape(record.external_reference)}" value="{escape(record.payer_email)}">
                </div>
                <div>
                  <label>Celular</label>
                  <input type="text" name="payer_phone::{escape(record.external_reference)}" value="{escape(record.payer_phone)}">
                </div>
              </div>
              <div class="muted small">Referencia interna: {escape(record.external_reference)}</div>
              <input type="hidden" name="selected_ref" value="{escape(record.external_reference)}">
            </div>
            """
        )

    return html_page(
        "Revisao antes do envio",
        f"""
        <div class="hero">
          <h1>Revisao final antes do Asaas</h1>
          <p>Confira os dados do pagador e os valores antes de seguir. Se algo vier incompleto no XML, voce pode corrigir aqui sem mexer no restante do lote.</p>
          {render_messages()}
        </div>
        <form method="post" action="/sync-selected" class="stack" style="margin-top:20px;">
          <div class="card">
            <div class="toolbar">
              <div style="min-width:260px; flex:1;">
                <label>Base URL do Asaas</label>
                <input type="text" name="asaas_base_url" value="{escape(asaas_base_url or DEFAULT_ASAAS_BASE_URL)}">
              </div>
              <div style="min-width:300px; flex:1;">
                <label>Access token do Asaas</label>
                <input type="password" name="asaas_access_token" value="{escape(asaas_access_token)}">
              </div>
            </div>
          </div>
          <div class="two-col">
            <div class="stack">{''.join(payload_boxes)}</div>
            <div class="stack">
              <div class="card">
                <div class="kpi">Selecionados</div>
                <div class="kpi-value">{len(selected_records)}</div>
                <div class="kpi-sub">Apenas esses registros serao enviados.</div>
              </div>
              <div class="card">
                <div class="kpi">Valor final selecionado</div>
                <div class="kpi-value">{escape(format_currency(sum(item.valor_final for item in selected_records)))}</div>
                <div class="kpi-sub">Se o desconto for confiavel, o envio vai com valor cheio + desconto percentual separado.</div>
              </div>
              <div class="card">
                <div class="kpi">Como ler esta tela</div>
                <div class="kpi-sub">Dry run faz uma simulacao e tenta conferir se a parcela ja existe no Asaas. Enviar selecionados cria somente o que estiver liberado.</div>
              </div>
              <div class="card stack">
                <button class="btn" type="submit" name="mode" value="dry-run">Executar dry run</button>
                <button class="btn warn" type="submit" name="mode" value="send">Enviar selecionados ao Asaas</button>
                <a class="btn secondary" href="/">Voltar para a lista</a>
              </div>
            </div>
          </div>
        </form>
        """,
    )


def render_dry_run_result(items: List[Dict[str, Any]], used_asaas_check: bool) -> str:
    new_count = sum(1 for item in items if item.get("suggested_action") == "Criar")
    existing_count = sum(1 for item in items if item.get("suggested_action") == "Ja existe no Asaas")
    blocked_count = sum(1 for item in items if item.get("suggested_action") == "Bloqueado localmente")

    rows = []
    for item in items:
        action = item.get("suggested_action")
        if action == "Criar":
            badge_class = "ok"
            row_class = "row-create"
        elif action == "Ja existe no Asaas":
            badge_class = "warn"
            row_class = "row-existing"
        else:
            badge_class = "bad"
            row_class = "row-blocked"

        asaas_state = item.get("asaas_exists")
        if asaas_state is True:
            asaas_text = "Ja existe no Asaas"
            asaas_class = "warn"
        elif asaas_state is False:
            asaas_text = "Nao encontrado"
            asaas_class = "ok"
        else:
            asaas_text = "Nao consultado"
            asaas_class = "secondary"

        rows.append(
            f"""
            <tr class="{row_class}">
              <td>
                <strong>{escape(item.get('aluno', '-'))}</strong><br>
                <span class="muted small">Matricula {escape(item.get('numero_matricula', '-'))}</span>
              </td>
              <td>{escape(item.get('due_date', '-'))}</td>
              <td>{escape(format_currency(float(item.get('valor_final') or 0.0)))}</td>
              <td><span class="pill {'warn' if item.get('local_already_sent') else 'ok'}">{escape('Ja enviado localmente' if item.get('local_already_sent') else 'Novo nesta maquina')}</span></td>
              <td><span class="pill {asaas_class}">{escape(asaas_text)}</span></td>
              <td><span class="pill {badge_class}">{escape(action or '-')}</span></td>
              <td class="muted small">{escape(item.get('detail', '-'))}</td>
            </tr>
            """
        )

    note = (
        "O dry run consultou o Asaas por externalReference para conferir se a parcela ja existe."
        if used_asaas_check
        else "O dry run nao consultou o Asaas porque nao havia token informado. A conferencia foi apenas local."
    )
    return html_page(
        "Dry Run",
        f"""
        <div class="hero">
          <h1>Previa do envio</h1>
          <p>{escape(note)}</p>
        </div>
        <div class="grid">
          <div class="card"><div class="kpi">Total analisado</div><div class="kpi-value">{len(items)}</div></div>
          <div class="card"><div class="kpi">Pronto para criar</div><div class="kpi-value">{new_count}</div></div>
          <div class="card"><div class="kpi">Ja existe no Asaas</div><div class="kpi-value">{existing_count}</div></div>
          <div class="card"><div class="kpi">Bloqueado</div><div class="kpi-value">{blocked_count}</div></div>
        </div>
        <div class="card" style="margin-top:20px;">
          <div class="mini-grid" style="margin-bottom:14px;">
            <div class="mini-box"><strong>Verde</strong>Vai criar normalmente</div>
            <div class="mini-box"><strong>Amarelo</strong>Parcela ja encontrada no Asaas</div>
            <div class="mini-box"><strong>Vermelho</strong>Bloqueada pelo historico local</div>
          </div>
          <div style="overflow:auto;">
            <table class="compact-table">
              <thead>
                <tr>
                  <th>Aluno</th>
                  <th>Vencimento</th>
                  <th>Valor</th>
                  <th>Historico local</th>
                  <th>Asaas</th>
                  <th>Acao sugerida</th>
                  <th>Observacao</th>
                </tr>
              </thead>
              <tbody>{''.join(rows)}</tbody>
            </table>
          </div>
        </div>
        <div style="margin-top:18px;">
          <a class="btn secondary" href="/">Voltar</a>
        </div>
        """,
    )


def render_sync_job(job_id: str, job: Dict[str, Any]) -> str:
    processed = int(job.get("processed") or 0)
    total = max(int(job.get("total") or 0), 1)
    percent = int((processed / total) * 100)
    status = job.get("status") or "running"
    is_running = status == "running"
    results = list(job.get("results") or [])[-10:]
    errors = list(job.get("errors") or [])[-10:]

    recent_items: List[str] = []
    for item in reversed(results):
        recent_items.append(
            f'<div class="record-box"><strong>{escape(item.get("title", "Resultado"))}</strong><div class="muted small">{escape(item.get("action", ""))} | payment {escape(item.get("payment_id", "-"))}</div></div>'
        )
    for item in reversed(errors):
        recent_items.append(f'<div class="message error">{escape(item)}</div>')

    refresh_tag = '<meta http-equiv="refresh" content="2">' if is_running else ""
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {refresh_tag}
  <title>Progresso do envio</title>
  <style>
    body {{ margin:0; font-family: Georgia, "Times New Roman", serif; background:#f4efe6; color:#1d2a30; }}
    .shell {{ max-width:1000px; margin:0 auto; padding:28px 20px 40px; }}
    .hero, .card, .record-box {{ background:#fffdf8; border:1px solid #d8cec2; border-radius:18px; padding:18px; box-shadow:0 10px 24px rgba(0,0,0,.04); }}
    .hero h1 {{ margin:0 0 10px; font-size:36px; }}
    .muted {{ color:#61727c; }}
    .grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:14px; margin:18px 0; }}
    .kpi {{ font-size:12px; text-transform:uppercase; letter-spacing:.08em; color:#61727c; margin-bottom:8px; }}
    .kpi-value {{ font-size:28px; font-weight:bold; }}
    .bar {{ height:18px; border-radius:999px; background:#eadfcf; overflow:hidden; }}
    .bar > div {{ height:100%; width:{percent}%; background:linear-gradient(90deg,#0e7490,#d97706); }}
    .message.error {{ padding:12px 14px; border-radius:14px; border:1px solid rgba(185,28,28,.25); background:rgba(185,28,28,.06); }}
    .stack > * + * {{ margin-top:12px; }}
    .btn {{ display:inline-flex; align-items:center; justify-content:center; min-height:44px; padding:0 18px; border-radius:999px; border:0; text-decoration:none; font-weight:bold; color:white; background:#39586a; }}
    @media (max-width: 900px) {{ .grid {{ grid-template-columns:1fr 1fr; }} }}
  </style>
</head>
<body>
  <div class="shell">
    <div class="hero">
      <h1>Enviando para o Asaas</h1>
      <p class="muted">O lote esta sendo processado em segundo plano. Esta pagina atualiza sozinha.</p>
      <p><strong>Status:</strong> {escape(status)}<br><strong>Atual:</strong> {escape(job.get("current_label") or "-")}</p>
      <div class="bar"><div></div></div>
      <p class="muted">{processed} de {int(job.get("total") or 0)} processado(s) - {percent}%</p>
    </div>
    <div class="grid">
      <div class="card"><div class="kpi">Total</div><div class="kpi-value">{int(job.get("total") or 0)}</div></div>
      <div class="card"><div class="kpi">Processados</div><div class="kpi-value">{processed}</div></div>
      <div class="card"><div class="kpi">Sucesso</div><div class="kpi-value">{int(job.get("success_count") or 0)}</div></div>
      <div class="card"><div class="kpi">Erros</div><div class="kpi-value">{int(job.get("error_count") or 0)}</div></div>
    </div>
    <div class="stack">
      {''.join(recent_items) if recent_items else '<div class="card muted">Aguardando os primeiros resultados...</div>'}
    </div>
    <div style="margin-top:18px;">
      <a class="btn" href="/">Voltar para o painel</a>
    </div>
  </div>
</body>
</html>"""


class RequestHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        route = urlparse(self.path).path
        if route == "/":
            self.respond_html(render_home())
            return
        if route == "/report":
            self.respond_report()
            return
        if route.startswith("/jobs/"):
            self.handle_job_status(route)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Rota nao encontrada.")

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        try:
            if route == "/load-xml":
                self.handle_load_xml()
                return
            if route == "/upload-xml":
                self.handle_upload_xml()
                return
            if route == "/save-settings":
                self.handle_save_settings()
                return
            if route == "/test-asaas":
                self.handle_test_asaas()
                return
            if route == "/review-selected":
                self.handle_review_selected()
                return
            if route == "/sync-selected":
                self.handle_sync_selected()
                return
            if route == "/cancel-missing":
                self.handle_cancel_missing()
                return
        except requests.HTTPError as exc:
            details = exc.response.text if exc.response is not None else str(exc)
            self.respond_html(
                html_page(
                    "Erro HTTP",
                    f'<div class="hero"><h1>Erro HTTP</h1><div class="message error">{escape(details)}</div><a class="btn secondary" href="/">Voltar</a></div>',
                ),
                status=500,
            )
            return
        except Exception as exc:
            self.respond_html(
                html_page(
                    "Erro inesperado",
                    f'<div class="hero"><h1>Erro inesperado</h1><div class="message error">{escape(str(exc))}</div><div class="mono">{escape(traceback.format_exc())}</div><a class="btn secondary" href="/">Voltar</a></div>',
                ),
                status=500,
            )
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Rota nao encontrada.")

    def handle_load_xml(self) -> None:
        xml_path = get_form_value(self, "xml_path").strip()
        if not xml_path:
            add_message("error", "Informe o caminho do XML.")
            self.redirect("/")
            return
        if not os.path.exists(xml_path):
            add_message("error", f"Arquivo nao encontrado: {xml_path}")
            self.redirect("/")
            return

        records, summary = parse_xml_records(xml_path)
        APP_STATE["xml_path"] = xml_path
        APP_STATE["records"] = records
        APP_STATE["records_by_reference"] = {item.external_reference: item for item in records}
        APP_STATE["summary"] = summary
        save_report(records, summary, xml_path)
        changes = update_snapshot_and_collect_changes(records, xml_path)
        logger.info("XML carregado por caminho | xml=%s | registros=%s", xml_path, len(records))
        add_message("success", f"XML carregado com sucesso: {len(records)} parcela(s) elegivel(is).")
        if changes["added"] or changes["missing"]:
            add_message("info", f"Comparacao com o XML anterior: {len(changes['added'])} entraram e {len(changes['missing'])} nao aparecem mais neste XML.")
        self.redirect("/")

    def handle_upload_xml(self) -> None:
        multipart = parse_multipart(self)
        upload = multipart.get("files", {}).get("xml_file")
        if not upload or not upload.get("content"):
            add_message("error", "Escolha um arquivo XML para anexar.")
            self.redirect("/")
            return

        xml_path = save_uploaded_xml(upload)
        records, summary = parse_xml_records(xml_path)
        APP_STATE["xml_path"] = xml_path
        APP_STATE["records"] = records
        APP_STATE["records_by_reference"] = {item.external_reference: item for item in records}
        APP_STATE["summary"] = summary
        save_report(records, summary, xml_path)
        changes = update_snapshot_and_collect_changes(records, xml_path)
        logger.info("XML carregado por upload | xml=%s | registros=%s", xml_path, len(records))
        add_message("success", f"XML anexado e lido com sucesso: {len(records)} parcela(s) elegivel(is).")
        if changes["added"] or changes["missing"]:
            add_message("info", f"Comparacao com o XML anterior: {len(changes['added'])} entraram e {len(changes['missing'])} nao aparecem mais neste XML.")
        self.redirect("/")

    def handle_save_settings(self) -> None:
        asaas_base_url = get_form_value(self, "asaas_base_url", DEFAULT_ASAAS_BASE_URL).strip() or DEFAULT_ASAAS_BASE_URL
        asaas_access_token = get_form_value(self, "asaas_access_token", "").strip()
        save_config(asaas_base_url, asaas_access_token)
        add_message("success", "Configuracao local do Asaas salva neste computador.")
        self.redirect("/")

    def handle_test_asaas(self) -> None:
        asaas_base_url = get_form_value(self, "asaas_base_url", DEFAULT_ASAAS_BASE_URL).strip() or DEFAULT_ASAAS_BASE_URL
        asaas_access_token = get_form_value(self, "asaas_access_token", "").strip()
        if not asaas_access_token:
            add_message("error", "Informe o token do Asaas para testar a conexao.")
            self.redirect("/")
            return
        client = AsaasClient(asaas_access_token, asaas_base_url)
        response = client.get("/customers", params={"limit": 1})
        add_message("success", f"Conexao com o Asaas funcionando. Total de clientes retornado: {response.get('totalCount')}.")
        self.redirect("/")

    def handle_review_selected(self) -> None:
        selected_refs = get_form_list(self, "selected_ref")
        if not selected_refs:
            add_message("error", "Selecione pelo menos uma parcela pronta para envio.")
            self.redirect("/")
            return

        records_by_reference = APP_STATE.get("records_by_reference", {})
        state = load_state()
        selected_records = [records_by_reference[ref] for ref in selected_refs if ref in records_by_reference]
        allowed_records, duplicate_records = filter_duplicate_records(selected_records, state)
        if duplicate_records:
            add_message("info", f"{len(duplicate_records)} parcela(s) ja enviadas foram ignoradas automaticamente.")
        if not allowed_records:
            add_message("error", "As parcelas selecionadas ja tinham sido enviadas e nao podem seguir novamente.")
            self.redirect("/")
            return
        config = load_config()
        self.respond_html(
            render_review(
                allowed_records,
                config.get("asaas_base_url") or DEFAULT_ASAAS_BASE_URL,
                config.get("asaas_access_token") or "",
            )
        )

    def handle_sync_selected(self) -> None:
        mode = get_form_value(self, "mode", "dry-run")
        selected_refs = get_form_list(self, "selected_ref")
        config = load_config()
        asaas_base_url = get_form_value(self, "asaas_base_url", config.get("asaas_base_url") or DEFAULT_ASAAS_BASE_URL).strip() or DEFAULT_ASAAS_BASE_URL
        asaas_access_token = get_form_value(self, "asaas_access_token", config.get("asaas_access_token") or "").strip()
        selected_records = replace_records_with_overrides(self, selected_refs)
        state = load_state()

        blocked = [item for item in selected_records if not item.ready_to_sync]
        if blocked:
            add_message("error", "Existem registros sem CPF, email ou telefone. Corrija esses campos antes de enviar.")
            self.respond_html(render_review(selected_records, asaas_base_url, asaas_access_token))
            return

        if mode == "dry-run":
            client = AsaasClient(asaas_access_token, asaas_base_url) if asaas_access_token else None
            inspected = inspect_records_with_asaas(selected_records, state, client)
            self.respond_html(render_dry_run_result(inspected, used_asaas_check=bool(client)))
            return

        if not asaas_access_token:
            add_message("error", "Informe o access token do Asaas para enviar.")
            self.respond_html(render_review(selected_records, asaas_base_url, asaas_access_token))
            return

        inspected = inspect_records_with_asaas(selected_records, state, AsaasClient(asaas_access_token, asaas_base_url))
        blocked_local = [item["record"] for item in inspected if item["suggested_action"] == "Bloqueado localmente"]
        blocked_asaas = [item["record"] for item in inspected if item["suggested_action"] == "Ja existe no Asaas"]
        selected_records = [item["record"] for item in inspected if item["suggested_action"] == "Criar"]

        if blocked_local:
            add_message("info", f"{len(blocked_local)} parcela(s) ja enviadas foram bloqueadas pelo historico local.")
        if blocked_asaas:
            add_message("info", f"{len(blocked_asaas)} parcela(s) foram bloqueadas porque ja existem no Asaas.")
        if not selected_records:
            self.redirect("/")
            return

        logger.info(
            "Inicio de sincronizacao | modo=%s | selecionados=%s | base_url=%s",
            mode,
            len(selected_records),
            asaas_base_url,
        )
        job_id = start_sync_job(selected_records, asaas_base_url, asaas_access_token)
        self.redirect(f"/jobs/{job_id}")

    def handle_cancel_missing(self) -> None:
        missing_refs = get_form_list(self, "missing_ref")
        config = load_config()
        asaas_base_url = get_form_value(self, "asaas_base_url", config.get("asaas_base_url") or DEFAULT_ASAAS_BASE_URL).strip() or DEFAULT_ASAAS_BASE_URL
        asaas_access_token = get_form_value(self, "asaas_access_token", config.get("asaas_access_token") or "").strip()

        if not missing_refs:
            add_message("error", "Selecione pelo menos uma cobranca ausente.")
            self.redirect("/")
            return
        if not asaas_access_token:
            add_message("error", "Informe o access token do Asaas para excluir os ausentes.")
            self.redirect("/")
            return

        client = AsaasClient(asaas_access_token, asaas_base_url)
        state = load_state()
        results = []
        errors = []
        for ref in missing_refs:
            try:
                missing_action = get_form_value(self, f"missing_action::{ref}", "school_paid")
                item = cancel_missing_payment(client, ref, state, missing_action)
                item["title"] = ref
                results.append(item)
                logger.info(
                    "Exclusao de ausente ok | external_reference=%s | payment_id=%s | customer_id=%s | action=%s",
                    ref,
                    item.get("payment_id"),
                    item.get("customer_id"),
                    item.get("action"),
                )
            except Exception as exc:
                logger.exception("Erro ao excluir ausente | external_reference=%s", ref)
                errors.append(f"{ref}: {exc}")
        save_state(state)
        self.respond_html(render_sync_result("Exclusao de ausentes concluida", results, errors))

    def handle_job_status(self, route: str) -> None:
        job_id = route.rsplit("/", 1)[-1].strip()
        with SYNC_JOBS_LOCK:
            job = SYNC_JOBS.get(job_id)
            job_copy = dict(job) if job else None
            if job_copy:
                job_copy["results"] = list(job.get("results") or [])
                job_copy["errors"] = list(job.get("errors") or [])
        if not job_copy:
            self.send_error(HTTPStatus.NOT_FOUND, "Job nao encontrado.")
            return
        self.respond_html(render_sync_job(job_id, job_copy))

    def respond_html(self, content: str, status: int = 200) -> None:
        data = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def respond_report(self) -> None:
        if not os.path.exists(REPORT_FILE):
            self.send_error(HTTPStatus.NOT_FOUND, "Relatorio ainda nao foi gerado.")
            return
        with open(REPORT_FILE, "rb") as file:
            data = file.read()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), RequestHandler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Servidor iniciado em {url}")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    server.serve_forever()


if __name__ == "__main__":
    main()
