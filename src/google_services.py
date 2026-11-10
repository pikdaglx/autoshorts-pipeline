import os
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = [
    # YouTube
    'https://www.googleapis.com/auth/youtube',
    'https://www.googleapis.com/auth/youtube.upload',
    'https://www.googleapis.com/auth/youtube.readonly',
    # Google Drive
    'https://www.googleapis.com/auth/drive',
    # Google Calendar
    'https://www.googleapis.com/auth/calendar',
    # Google Sheets
    'https://www.googleapis.com/auth/spreadsheets',
    # Google Docs
    'https://www.googleapis.com/auth/documents',
    # Google Slides
    'https://www.googleapis.com/auth/presentations',
]

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_PATH = os.path.join(BASE_DIR, 'token.json')
CLIENT_SECRET_PATH = os.path.join(BASE_DIR, 'client_secret.json')

def get_credentials() -> Credentials:
    """Carrega as credenciais autenticadas do token.json (ou env var) e renova se necessário."""
    if not os.path.exists(TOKEN_PATH) and os.getenv("GOOGLE_TOKEN_JSON"):
        with open(TOKEN_PATH, 'w', encoding='utf-8') as f:
            f.write(os.getenv("GOOGLE_TOKEN_JSON", ""))

    if not os.path.exists(CLIENT_SECRET_PATH) and os.getenv("GOOGLE_CLIENT_SECRET_JSON"):
        with open(CLIENT_SECRET_PATH, 'w', encoding='utf-8') as f:
            f.write(os.getenv("GOOGLE_CLIENT_SECRET_JSON", ""))

    if not os.path.exists(TOKEN_PATH):
        raise FileNotFoundError(f"Arquivo '{TOKEN_PATH}' nao encontrado. Faca o login inicial primeiro.")

    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds.valid:
        if creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as e:
                raise RuntimeError(
                    f"Token de acesso expirado e nao foi possivel renovar: {e}\n"
                    f"Execute 'python -m src.cli login' para reconectar sua conta."
                ) from e
            try:
                with open(TOKEN_PATH, 'w', encoding='utf-8') as token:
                    token.write(creds.to_json())
            except Exception:
                pass
        else:
            raise RuntimeError("Token expirado sem refresh_token.")
    return creds


def run_oauth_login():
    """Executa o fluxo OAuth interativo para conectar uma conta Google."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    if not os.path.exists(CLIENT_SECRET_PATH):
        raise FileNotFoundError(
            f"Arquivo '{CLIENT_SECRET_PATH}' nao encontrado.\n"
            f"Baixe o arquivo de credenciais OAuth no Google Cloud Console.\n"
            f"https://console.cloud.google.com/apis/credentials"
        )
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_PATH, SCOPES)
    creds = flow.run_local_server(port=0, prompt='consent', access_type='offline')
    with open(TOKEN_PATH, 'w', encoding='utf-8') as token:
        token.write(creds.to_json())
    return creds


class YouTubeService:
    """Gerenciamento e upload para o YouTube."""

    def __init__(self, creds: Optional[Credentials] = None):
        self.creds = creds or get_credentials()
        self.service = build('youtube', 'v3', credentials=self.creds)

    def upload_video(
        self,
        file_path: str,
        title: str,
        description: str,
        tags: Optional[List[str]] = None,
        privacy_status: str = "private",
        publish_at: Optional[datetime] = None,
        made_for_kids: bool = False
    ) -> Dict[str, Any]:
        """Faz upload de um Shorts para o YouTube."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Video nao encontrado: {file_path}")

        status_body: Dict[str, Any] = {
            'privacyStatus': privacy_status,
            'selfDeclaredMadeForKids': made_for_kids
        }

        if publish_at:
            status_body['privacyStatus'] = 'private'
            status_body['publishAt'] = publish_at.strftime('%Y-%m-%dT%H:%M:%S.000Z')

        clean_tags = tags or ["cortes", "podcast", "shorts", "viral"]

        body = {
            'snippet': {
                'title': title[:100],
                'description': description,
                'tags': clean_tags,
                'categoryId': '24'  # Entertainment / Entretenimento
            },
            'status': status_body
        }

        media = MediaFileUpload(
            file_path,
            chunksize=-1,
            resumable=True,
            mimetype='video/mp4'
        )

        request = self.service.videos().insert(
            part=','.join(body.keys()),
            body=body,
            media_body=media
        )

        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                print(f"[*] Progresso do upload: {int(status.progress() * 100)}%", flush=True)

        return response

    def delete_video(self, video_id: str) -> bool:
        """Exclui um vídeo do YouTube via API."""
        try:
            self.service.videos().delete(id=video_id).execute()
            print(f"[🗑️] Vídeo {video_id} excluído com sucesso do canal.", flush=True)
            return True
        except Exception as e:
            print(f"[!] Erro ao excluir vídeo {video_id}: {e}", flush=True)
            return False
