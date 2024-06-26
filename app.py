from flask import Flask, jsonify, request
import random 
import datetime 
import uuid
app = Flask(__name__)


data = {
        "id": str(uuid.uuid1()),
        "student": random.choice(["a", "b", "c"]), 
        "score": str(random.uniform(0,100)),
        "test": random.choice(["x","y","z"]),
        "created_at": str(datetime.datetime.now())
    }

@app.route('/api/data', methods=['GET'])
def get_data():
    return jsonify(data)


if __name__ == '__main__':
    app.run(debug=True, port=4545)