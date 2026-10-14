import uvicorn

if __name__ == "__main__":
    print("📖 Swagger API Documentation: http://localhost:8000/docs")
    print("🩺 Health Check: http://localhost:8000/health")
    print("⚡ Powered by equalsignorecase")
    uvicorn.run("api.index:app", host="0.0.0.0", port=8000, reload=True)
