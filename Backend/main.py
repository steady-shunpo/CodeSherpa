from fastapi import FastAPI


app = FastAPI()


@app.get('/')
def test():
    print("test works")

