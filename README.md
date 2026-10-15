# Employee Attendance & Analytics API

FastAPI + MongoDB implementation draft for the HROne Software Engineer Trainee assignment.

## Run locally

- Python 3.11+ and MongoDB 6.0+ are required.
- Set `MONGO_URI` and `MONGO_DB` in environment variables or a local `.env` file. Never commit `.env` or credentials.
- Install dependencies: `pip install -r requirements.txt`
- Start: `uvicorn app.main:app --port 8000`
- Interactive API docs: `http://127.0.0.1:8000/docs`

Indexes are created automatically at application startup. This is a draft: validate every endpoint against the supplied `openapi.yaml`, test concurrency and analytics against MongoDB 6.0+, and update this README with any remaining limitations before submitting. The hidden grader has not been run against this code.
