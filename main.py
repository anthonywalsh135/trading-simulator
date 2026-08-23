#the entry point. run the site with:  python main.py
#
#every setting, including whether debug mode is on, comes from .env. see
#.env.example for the keys that are needed.

from website import make_app
from website.config import Config

app = make_app()


if __name__ == "__main__":
    app.run(debug=Config.DEBUG, host="127.0.0.1", port=5000)
