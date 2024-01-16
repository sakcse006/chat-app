from fastapi import FastAPI, HTTPException, Depends, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import Column, Integer, String, create_engine, DateTime, Text, ForeignKey, or_
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
import os
from fastapi.security import OAuth2PasswordBearer
from datetime import datetime, timedelta
import jwt
import boto3
from datetime import datetime
from fastapi.responses import FileResponse

basedir = os.path.abspath(os.path.dirname(__file__))

DATABASE_URL = "sqlite:///"+os.path.join(basedir, "socket.db")
Base = declarative_base()
SECRET_KEY = "12345"


class User(Base):

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, index=True)
    email = Column(String, unique=True, index=True)
    password = Column(String, index=True)
    websocket = Column(String, default=None)

    sent_messages = relationship("Chat", back_populates="sender", foreign_keys="[Chat.sender_id]")

    # Define receiver relationship
    received_messages = relationship("Chat", back_populates="receiver", foreign_keys="[Chat.receiver_id]")


class Chat(Base):

    __tablename__ = "chat"

    id = Column(Integer, primary_key=True, index=True)
    message = Column(Text)
    type = Column(String, index=True)
    datetime = Column(DateTime, default=datetime.utcnow)
    url = Column(String, default=None)
    sender_id = Column(Integer, ForeignKey("users.id"))
    receiver_id = Column(Integer, ForeignKey("users.id"))

    sender = relationship("User", back_populates="sent_messages", foreign_keys=[sender_id])
    receiver = relationship("User", back_populates="received_messages", foreign_keys=[receiver_id])


engine = create_engine(DATABASE_URL)
Base.metadata.create_all(bind=engine)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

app = FastAPI()

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Update this to the specific origins you want to allow
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")
blacklisted_tokens = []


@app.get("/")
def read_root():
    return FileResponse("index.html")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_jwt_token(data: dict, expires_delta: timedelta):
    to_encode = data.copy()
    expire = datetime.utcnow() + expires_delta
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm="HS256")
    return encoded_jwt


# Function to get the current user based on the token
def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=401,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        if token in blacklisted_tokens:
            raise credentials_exception
        return payload
    except jwt.ExpiredSignatureError:
        raise credentials_exception
    except jwt.InvalidTokenError:
        raise credentials_exception


# Store active connections
connections = []


@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int):
    await websocket.accept()
    connections.append({"user_id": user_id, "websocket": websocket})
    try:
        while True:
            # Receive message from the client
            data = await websocket.receive_text()
            receiver_user = int(data.split('@')[0])
            message = data.split('@')[1]
            chat_message(message, user_id, receiver_user)
            for conn in connections:
                if conn['user_id'] == receiver_user:
                    receiver_websocket = conn['websocket']
                    await receiver_websocket.send_text(message)
    except WebSocketDisconnect:
        for conn in connections:
            if conn['user_id'] == user_id:
                connections.remove(conn)


def chat_message(data: str, sender: int, receiver: int, db: Session = Depends(get_db)):
    try:
        chat_instance = Chat(message=data, type="text", url=None, sender_id=sender, receiver_id=receiver)
        session = SessionLocal()
        session.add(chat_instance)
        session.commit()
    except Exception as e:
        raise Exception(e.args)


@app.post("/messages/")
async def get_messages(payload: dict, db: Session = Depends(get_db)):
    try:
        if payload['type'] == "all":
            chat = db.query(Chat).filter(Chat.sender_id.in_([payload['sender'], payload['receiver']]), Chat.receiver_id.in_([payload['sender'], payload['receiver']])).all()
            return {"status": "success", "data": chat}
        else:
            chat = db.query(Chat).filter(Chat.sender_id == payload['sender'], Chat.receiver_id == payload['receiver']).order_by(-Chat.id).first()
            return {"status": "success", "data": chat}
    except Exception as e:
        raise Exception(e.args)


@app.post("/message-delete/")
async def message_delete(payload: dict, db: Session = Depends(get_db)):
    try:
        record = db.query(Chat).filter(Chat.id == payload['chat_id']).first()
        db.delete(record)
        db.commit()
        return {"status": "success", "message": "Delete Successfully"}
    except Exception as e:
        raise Exception(e.args)


@app.post("/create-user/")
def create_user(user: dict, db: Session = Depends(get_db)):
    print(user)
    new_user = User(username=user['user_name'], email=user['email'], password=user['password'])
    session = SessionLocal()
    session.add(new_user)
    session.commit()
    # user = db.query(User).all()
    # return {"data": user}
    return JSONResponse({"status": "success", "message": "user created successfully"})


@app.get("/get-user/")
def get_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    payload = get_current_user(token)
    # user = db.query(User).filter(User.websocket != None, User.id != payload['user_id']).all()
    user = db.query(User).filter(User.id != payload['user_id']).all()
    return {"status": "success", "data": user, 'profile': payload}


@app.post("/verify-user/")
def verify_user(user: dict, db: Session = Depends(get_db)):
    user_instance = db.query(User).filter(User.email == user['email'], User.password == user['password'])
    if db.query(user_instance.exists()).scalar():
        token_data = {"user_id": user_instance[0].id, "username": user_instance[0].username, "email": user_instance[0].email}
        access_token_expires = timedelta(days=7)
        access_token = create_jwt_token(data=token_data, expires_delta=access_token_expires)
        return {"status": "success", "access_token": access_token, "token_type": "bearer"}
    else:
        return {"status": "failure", "message": "Invalid credential"}


@app.get("/users/{user_id}")
def read_user(user_id: str, db: Session = Depends(get_db)):
    print(user_id)
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        return user
    raise HTTPException(status_code=404, detail="User not found")


# Route that requires a valid JWT token
@app.post("/protected/")
async def protected_route(user: dict, token: str = Depends(oauth2_scheme)):
    print(user)
    payload = get_current_user(token)
    return {"message": "Hello, {}!".format(payload["sub"])}


@app.post("/logout/")
async def logout(user: dict, token: str = Depends(oauth2_scheme)):
    print(user)
    blacklisted_tokens.append(token)
    return {"status": "success", "message": "logout successfully"}


# @app.post("/storage/")
# def s3_storage(data: dict):
#     # Replace 'your-access-key-id' and 'your-secret-access-key' with your AWS credentials
#     aws_access_key_id = 'AKIA3SAFTVARWU7REWNX'
#     aws_secret_access_key = 'bJ+UXl6nJ9BhN3n569X4LPGjhBBJElVls6XLuZnF'
#
#     # Replace 'your-bucket-name' with the name of your S3 bucket
#     bucket_name = 's3-centos'
#     image_key = 'artificial-intelligence-ai-icon.png'
#
#     # Create an S3 client
#     s3 = boto3.client('s3', aws_access_key_id=aws_access_key_id, aws_secret_access_key=aws_secret_access_key)
#
#     # response = s3.get_object(Bucket=bucket_name, Key=image_key)
#     # image_data = response['Body'].read()
#
#     params = {
#         'Bucket': bucket_name,
#         'Key': "nature.mp4"
#     }
#
#     signed_url = s3.generate_presigned_url('get_object', Params=params)
#
#     return JSONResponse({'signedUrl': signed_url})
#
#
#     # Example: List objects in the bucket
#     # response = s3.list_objects(Bucket=bucket_name)
#     #
#     # # Print the object keys
#     # for obj in response.get('Contents', []):
#     #     print(obj['Key'])

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)

