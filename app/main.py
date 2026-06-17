from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from app.agents.name_agent import screen_business_name

app = FastAPI(title="AI Business Launcher")

templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(request, "index.html")

@app.post("/screen-name", response_class=HTMLResponse)
async def screen_name(
    request: Request,
    business_idea: str = Form(...),
    state: str = Form(...)
):
    result = screen_business_name(business_idea, state)
    return templates.TemplateResponse(request, "result.html", {"result": result})

@app.get("/health")
def health():
    return {"status": "ok"}
