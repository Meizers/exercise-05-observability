import time
from datetime import datetime, timezone
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import text
from sqlalchemy.orm import Session
from src.database import Base, engine, get_db
from src.models import Node
from src.schemas import NodeCreate, NodeResponse, NodeUpdate

Base.metadata.create_all(bind=engine)
app = FastAPI()

REQUESTS = Counter("http_requests_total", "HTTP requests handled", ["method", "endpoint", "status"])
DURATION = Histogram("http_request_duration_seconds", "HTTP request latency", ["method", "endpoint"])
ACTIVE_NODES = Gauge("active_nodes", "Registered nodes with status active")

@app.middleware("http")
async def track_requests(request: Request, call_next):
    start = time.perf_counter()
    status = 500  # kept if the handler raises
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        # Label by route template (/api/nodes/{name}), not raw path, to keep cardinality bounded.
        route = request.scope.get("route")
        endpoint = route.path if route else "unmatched"
        DURATION.labels(request.method, endpoint).observe(time.perf_counter() - start)
        REQUESTS.labels(request.method, endpoint, str(status)).inc()

@app.get("/metrics")
def metrics(db: Session = Depends(get_db)):
    try:
        ACTIVE_NODES.set(db.query(Node).filter(Node.status == "active").count())
    except Exception:
        pass  # still serve metrics while the db is down; the gauge keeps its last value
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception:
        db_status = "disconnected"
    count = db.query(Node).filter(Node.status == "active").count()
    return {"status": "ok", "db": db_status, "nodes_count": count}

@app.post("/api/nodes", response_model=NodeResponse, status_code=201)
def register_node(node: NodeCreate, db: Session = Depends(get_db)):
    existing = db.query(Node).filter(Node.name == node.name).first()
    if existing:
        raise HTTPException(status_code=409, detail="Node already exists")
    db_node = Node(name=node.name, host=node.host, port=node.port)
    db.add(db_node)
    db.commit()
    db.refresh(db_node)
    return db_node

@app.get("/api/nodes", response_model=list[NodeResponse])
def list_nodes(db: Session = Depends(get_db)):
    return db.query(Node).all()

@app.get("/api/nodes/{name}", response_model=NodeResponse)
def get_node(name: str, db: Session = Depends(get_db)):
    node = db.query(Node).filter(Node.name == name).first()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    return node

@app.put("/api/nodes/{name}", response_model=NodeResponse)
def update_node(name: str, update: NodeUpdate, db: Session = Depends(get_db)):
    node = db.query(Node).filter(Node.name == name).first()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    if update.host is not None:
        node.host = update.host
    if update.port is not None:
        node.port = update.port
    node.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(node)
    return node

@app.delete("/api/nodes/{name}", status_code=204)
def delete_node(name: str, db: Session = Depends(get_db)):
    node = db.query(Node).filter(Node.name == name).first()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    node.status = "inactive"
    node.updated_at = datetime.now(timezone.utc)
    db.commit()
    return Response(status_code=204)
