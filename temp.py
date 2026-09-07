from werkzeug.security import generate_password_hash

print(generate_password_hash("#Viama_2023", method="scrypt"))