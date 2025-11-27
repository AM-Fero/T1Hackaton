import logging
from flask import Flask, request, jsonify, render_template, redirect
from flask_cors import CORS
import psycopg2
import json
import hashlib
import base64
import threading
import uuid
import time
from openai import OpenAI
import code_runner
import random

# Конфигурации
API_KEY = "sk-k0R7hQEtJ_D6tQ64IvDfsQ"
BASE_URL = "https://llm.t1v.scibox.tech/v1"

# Инициализация Flask приложения
app = Flask(__name__)
CORS(app)

# Настройка логирования
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Хранилище активных сессий чата
chat_sessions = {}
session_lock = threading.Lock()

class ChatSession:
    def __init__(self, session_id, vacancy_data=None, application_data=None):
        self.session_id = session_id
        self.vacancy_data = vacancy_data or {}
        self.application_data = application_data or {}
        self.messages = [
            {"role": "system", "content": f"""
            Ты HR, проводящий технические собеседования с кандидатом.
            
            Информация о вакансии:
            - Должность: {vacancy_data.get('Name', 'Не указано')}
            - Описание: {vacancy_data.get('Description', 'Не указано')}
            
            Информация о кандидате:
            - Имя: {application_data.get('firstName', 'Unknown')} {application_data.get('lastName', 'Unknown')}
            Тебе заранее ничего не известно о его опыте работы и проектах, в которых он участвовал. 

            Информация об уровне необходимых знаний:
            - Грейд: {vacancy_data['Difficulty']}
            
            Твоя роль быть объективным, спокойным, уверенным, профессиональным. Не уходи от темы, задавай уточняющие вопросы, но не слишком много.
            """}
        ]
        self.created_at = time.time()
        self.last_activity = time.time()
        self.question_count = 0
        self.current_question = 0
        self.question_types = -1
        self.code_task_data = {}
    
    def add_message(self, role, content):
        self.messages.append({"role": role, "content": content})
        self.last_activity = time.time()
    
    def get_messages(self):
        return self.messages
    
    def cleanup_old_messages(self, max_messages=20):
        """Сохраняет только последние сообщения для предотвращения проблем с памятью"""
        if len(self.messages) > max_messages:
            # Сохраняем системное сообщение и последние сообщения
            system_msg = self.messages[0]
            recent_msgs = self.messages[-max_messages+1:]
            self.messages = [system_msg] + recent_msgs

def get_chat_session(session_id, vacancy_data=None, application_data=None):
    """Получает или создает сессию чата с контекстом вакансии и заявки"""
    with session_lock:
        if session_id not in chat_sessions:
            chat_sessions[session_id] = ChatSession(session_id, vacancy_data, application_data)
        return chat_sessions[session_id]

def cleanup_inactive_sessions(max_inactive_time=3600):  # 1 час
    """Очищает неактивные сессии"""
    current_time = time.time()
    sessions_to_remove = []
    
    with session_lock:
        for session_id, session in chat_sessions.items():
            if current_time - session.last_activity > max_inactive_time:
                sessions_to_remove.append(session_id)
        
        for session_id in sessions_to_remove:
            del chat_sessions[session_id]
    
    if sessions_to_remove:
        logger.info(f"Очищено {len(sessions_to_remove)} неактивных сессий")

# Функция для загрузки конфигурации из JSON файла
def load_db_config():
    try:
        with open('dbconfig.json', 'r', encoding='utf-8') as config_file:
            config = json.load(config_file)
            logger.debug('Конфигурация базы данных успешно загружена из config.json')
            return config
    except FileNotFoundError:
        logger.error('Файл config.json не найден')
        raise
    except json.JSONDecodeError as e:
        logger.error(f'Ошибка парсинга config.json: {e}')
        raise
    except Exception as e:
        logger.error(f'Неизвестная ошибка при загрузке конфигурации: {e}')
        raise

# Загружаем конфигурацию БД
try:
    DB_CONFIG = load_db_config()
except Exception as e:
    logger.error(f'Не удалось загрузить конфигурацию БД: {e}')
    DB_CONFIG = {}

# Установка соединения с PostgreSQL
def get_db_connection():
    logger.debug('Попытка подключения к базе данных...')
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        logger.debug('Подключение к базе данных успешно установлено')
        return conn
    except Exception as e:
        logger.error(f"Ошибка подключения к базе данных: {e}")
        return None

# Функция для генерации уникального 8-символьного хеша на основе ID вакансии
def generate_vacancy_hash(vacancy_id):
    """Генерирует уникальную 8-символьную строку на основе ID вакансии"""
    id_str = str(vacancy_id)
    hash_object = hashlib.sha256(id_str.encode())
    hex_digest = hash_object.hexdigest()[:12]
    base64_encoded = base64.urlsafe_b64encode(hex_digest.encode()).decode()
    short_hash = ''.join(c for c in base64_encoded[:8] if c.isalnum())
    
    if len(short_hash) < 8:
        additional_hash = hashlib.md5(id_str.encode()).hexdigest()[:8-len(short_hash)]
        short_hash += additional_hash
    
    return short_hash[:8]

# Глобальный обработчик CORS
@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', 'http://127.0.0.1:5500')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    response.headers.add('Access-Control-Allow-Credentials', 'true')
    return response

# HTML Template Endpoints
@app.route('/vacancy/<string:vacancy_hash>')
def vacancy_page(vacancy_hash):
    """
    Рендерит HTML страницу для конкретной вакансии используя ее хеш
    """
    logger.info(f"Рендеринг страницы вакансии для хеша: {vacancy_hash}")
    
    if len(vacancy_hash) != 8:
        return render_template('vacancy_template.html', 
                             error='Неверный формат ссылки на вакансию'), 400
    
    conn = get_db_connection()
    if not conn:
        return render_template('vacancy_template.html', 
                             error='Ошибка подключения к базе данных'), 500
    
    try:
        cursor = conn.cursor()
        
        # Получаем все вакансии для поиска совпадающего хеша
        cursor.execute('SELECT "ID", "ID_Company", "ID_InterviewType", "Name", "Description", "TasksCount", "TasksType" FROM public."Vacancies"')
        
        columns = [desc[0] for desc in cursor.description]
        
        for row in cursor.fetchall():
            vacancy = dict(zip(columns, row))
            # Генерируем хеш для этого ID вакансии
            current_hash = generate_vacancy_hash(vacancy['ID'])
            
            # Если хеш совпадает, рендерим шаблон с данными вакансии
            if current_hash == vacancy_hash:
                logger.info(f"Найдена вакансия ID {vacancy['ID']} для хеша {vacancy_hash}")
                return render_template('vacancy_template.html', 
                                     vacancy=vacancy,
                                     vacancy_hash=vacancy_hash)
        
        # Если вакансия с таким хешем не найдена
        logger.warning(f"Вакансия с хешем не найдена: {vacancy_hash}")
        return render_template('vacancy_template.html', 
                             error='Вакансия не найдена или была удалена'), 404
        
    except Exception as e:
        logger.error(f"Ошибка при рендеринге страницы вакансии: {e}")
        return render_template('vacancy_template.html', 
                             error='Произошла ошибка при загрузке вакансии'), 500
    finally:
        if conn:
            conn.close()

@app.route('/vacancy/id/<int:vacancy_id>')
def vacancy_page_by_id(vacancy_id):
    """
    Перенаправляет на URL с хешем для вакансии
    """
    hash_value = generate_vacancy_hash(vacancy_id)
    return redirect(f'/vacancy/{hash_value}')

@app.route('/')
def vacancies_list_page():
    """
    Домашняя страница показывающая все доступные вакансии
    """
    conn = get_db_connection()
    if not conn:
        return render_template('vacancies_list.html', 
                             error='Ошибка подключения к базе данных')
    
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM public."Vacancies" ORDER BY "ID"')
        
        columns = [desc[0] for desc in cursor.description]
        vacancies = []
        
        for row in cursor.fetchall():
            vacancy = dict(zip(columns, row))
            vacancy['hash'] = generate_vacancy_hash(vacancy['ID'])
            vacancies.append(vacancy)
        
        return render_template('vacancies_list.html', vacancies=vacancies)
        
    except Exception as e:
        logger.error(f"Ошибка получения списка вакансий: {e}")
        return render_template('vacancies_list.html', 
                             error='Не удалось загрузить вакансии')
    finally:
        if conn:
            conn.close()

# Interview Session Endpoints
@app.route('/api/start-interview', methods=['POST'])
def start_interview():
    """
    Начинает новую сессию собеседования с информацией о вакансии и данными кандидата
    Ожидаемый JSON payload:
    {
        "vacancyId": 1,
        "vacancyHash": "qw93nfeq",
        "firstName": "John",
        "lastName": "Doe",
        "phoneNumber": "+1234567890",
        "resumeLink": "https://example.com/resume.pdf",
        "coverLetter": "Optional cover letter text"
    }
    """
    logger.info("Получен запрос на начало сессии собеседования")
    
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({'error': 'JSON данные не предоставлены'}), 400
        
        # Проверяем обязательные поля
        required_fields = ['vacancyId', 'vacancyHash', 'firstName', 'lastName', 'phoneNumber', 'resumeLink']
        for field in required_fields:
            if field not in data:
                return jsonify({'error': f'Отсутствует обязательное поле: {field}'}), 400
        
        # Получаем данные вакансии из базы данных
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'Ошибка подключения к базе данных'}), 500
        
        try:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT "ID", "ID_Company", "ID_InterviewType", "Name", "Description", "TasksCount", "TasksType" FROM public."Vacancies" WHERE "ID" = %s',
                (data['vacancyId'],)
            )
            
            vacancy_row = cursor.fetchone()
            if not vacancy_row:
                return jsonify({'error': 'Вакансия не найдена'}), 404
            
            columns = ['ID', 'ID_Company', 'ID_InterviewType', 'Name', 'Description', 'TasksCount', 'TasksType']
            vacancy_data = dict(zip(columns, vacancy_row))

            ### TODO Переделать это, не хардкодить
            vacancy_data['TasksCount'] = 2
            vacancy_data['TasksType'] = -1
            vacancy_data['Difficulty'] = 'Junior'
            ### TODO
            
            # Проверяем совпадение хеша
            expected_hash = generate_vacancy_hash(data['vacancyId'])
            if data['vacancyHash'] != expected_hash:
                return jsonify({'error': 'Неверный хеш вакансии'}), 400
            
        except Exception as e:
            logger.error(f"Ошибка базы данных: {e}")
            return jsonify({'error': 'Не удалось получить данные вакансии'}), 500
        finally:
            if conn:
                conn.close()
        
        # Подготавливаем данные заявки
        application_data = {
            'firstName': data['firstName'],
            'lastName': data['lastName'],
            'phoneNumber': data['phoneNumber'],
            'resumeLink': data['resumeLink']
        }
        
        # Создаем новую сессию с контекстом
        session_id = str(uuid.uuid4())
        session = get_chat_session(session_id, vacancy_data, application_data)
        session.question_count = vacancy_data['TasksCount']
        session.current_question = 0
        session.question_types = vacancy_data['TasksType']
        
        # Генерируем начальное приветствие от AI
        client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
        
        if session.question_types == -1:
            initial_prompt = f"""
            Поприветствуй кандидата {application_data['firstName']} {application_data['lastName']} и начни техническое собеседование на 
            должность {vacancy_data['Name']}.
            Будь дружелюбным, но профессиональным. Задай первый вопрос по трудности на {vacancy_data['Difficulty']} позицию. Вопрос должен
            быть теоретическим, довольно общим. Не давай кандидату переключаться с вопроса. Тебя зовут Василий."""

        # Ask your first question to understand their background and interest in this role.
        # Keep it concise - one or two sentences for greeting and one relevant question.
        
        resp = client.chat.completions.create(
            model="qwen3-coder-30b-a3b-instruct-fp8",
            messages=session.get_messages() + [{"role": "user", "content": initial_prompt}],
            temperature=0.4,
            max_tokens=150,
        )
        
        initial_response = resp.choices[0].message.content
        session.add_message("assistant", initial_response)
        
        logger.info(f"Создана сессия собеседования {session_id} для вакансии {data['vacancyId']}")
        
        return jsonify({
            'session_id': session_id,
            'initial_message': initial_response,
            'vacancy': vacancy_data['Name'],
            'applicant': f"{application_data['firstName']} {application_data['lastName']}",
            'timestamp': time.time()
        }), 201
        
    except Exception as e:
        logger.error(f"Ошибка начала собеседования: {e}")
        return jsonify({'error': 'Не удалось начать сессию собеседования'}), 500

# Chat endpoint
@app.route('/api/chat', methods=['POST'])
def chat():
    """
    Продолжает сессию чата
    Ожидаемый JSON:
    {
        "session_id": "uuid-string",
        "message": "user message"
    }
    """
    try:
        data = request.json
        message = data.get('message', '').strip()
        session_id = data.get('session_id', '')
        
        if not message:
            return jsonify({'error': 'Пустое сообщение'}), 400
        
        if not session_id:
            return jsonify({'error': 'Требуется ID сессии'}), 400
        
        # Получаем сессию
        with session_lock:
            if session_id not in chat_sessions:
                return jsonify({'error': 'Сессия не найдена'}), 404
            session = chat_sessions[session_id]
        
        
        session.add_message("user", message)
        
        # Получаем ответ от чат-бота
        client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
        
        resp = client.chat.completions.create(
            model="qwen3-coder-30b-a3b-instruct-fp8",
            messages=session.get_messages(),
            temperature=0.5,
            top_p=0.9,
            max_tokens=1000,
        )
        
        bot_response = resp.choices[0].message.content
        
        # Добавляем ответ бота в сессию
        session.add_message("assistant", bot_response)
        # session.cleanup_old_messages()

        separator = '%%C%%'
        result_string = separator.join(str(item) for item in session.get_messages())

        client_validity = OpenAI(api_key=API_KEY, base_url=BASE_URL)
        resp_validity = client_validity.chat.completions.create(
            model="qwen3-32b-awq",
            messages=[
            {"role": "system", "content": "Ты аналитик - четкий, объективный, исполнительный."},
            {"role": "user", "content": f"""Дай оценку, а именно просто 2 числа от 0 до 100, на сколько можно считать текущий
                        вопрос интервью оконченным и на сколько хорошо кандидат ответил на него. Первое число означает процент
                        законченности, чем оно выше, тем лучше перейти к следующему вопросу в собеседовании. Если ты понимаешь, что разговор затянулся,
                        а именно кандидат дал полный ответ или было крайне много уточняющих вопросов, не стесняйся повышать первое число. Второе число означает качество ответа
                        кандидата на вопрос, чем оно выше - тем лучше ответы кандидата. Если кандидат не отвечает на вопрос, пишет случайный текст, пытается
                        перевести тему, первое число следует повысить, второе понизить. В качестве ответа
                        на этот промпт пришли ТОЛЬКО 2 ЧИСЛА И НИЧЕГО БОЛЕЕ Вот полная переписка общения кандитада с интервюером на текущий момент:
                        {result_string}"""},
                ],
            temperature=0.3,
            top_p=0.9,
            max_tokens=1000,
        )
        validity_asessment = resp_validity.choices[0].message.content
        print("`````````````````````````````")
        assesment = list(validity_asessment.split('</think>')[1].strip().split(" "))
        print("Оценка завершенности и качества:", assesment)
        changeQuestion = False
        NextQuestionType = -1
        isDone = False

        if (int(assesment[0]) >= 80):
            changeQuestion = True
            session.current_question += 1
            if session.current_question == session.question_count:
                isDone = True
        if session.current_question %2 == 0:
            NextQuestionType = 0
        else:
            NextQuestionType = 1
        return jsonify({
            'response': bot_response,
            'session_id': session_id,
            'timestamp': time.time(),
            'changeQuestion': changeQuestion,
            'nextQuestionType': NextQuestionType,
            'isDone': isDone,
            'currentQuestion': session.current_question
        })
        
    except Exception as e:
        logger.error(f"Ошибка в chat endpoint: {str(e)}")
        return jsonify({'error': 'Внутренняя ошибка сервера'}), 500

@app.route('/api/session/<session_id>', methods=['GET'])
def get_session_info(session_id):
    """Получает информацию о сессии"""
    with session_lock:
        if session_id in chat_sessions:
            session = chat_sessions[session_id]
            return jsonify({
                'session_id': session_id,
                'vacancy': session.vacancy_data.get('Name'),
                'applicant': f"{session.application_data.get('firstName', '')} {session.application_data.get('lastName', '')}",
                'created_at': session.created_at,
                'last_activity': session.last_activity,
                'message_count': len(session.messages) - 1  # исключаем системное сообщение
            })
        else:
            return jsonify({'error': 'Сессия не найдена'}), 404

@app.route('/api/session/<session_id>', methods=['DELETE'])
def delete_session(session_id):
    """Удаляет сессию чата"""
    with session_lock:
        if session_id in chat_sessions:
            del chat_sessions[session_id]
            logger.info(f"Удалена сессия {session_id}")
            return jsonify({'message': 'Сессия удалена'})
        else:
            return jsonify({'error': 'Сессия не найдена'}), 404

# API Endpoints for vacancies
@app.route('/vacancies', methods=['POST'])
def add_vacancy():
    """
    Добавляет новую вакансию в таблицу Vacancies
    Ожидаемый JSON payload:
    {
        "ID_Company": 1,
        "ID_InterviewType": 1,
        "Name": "Backend Developer",
        "Description": "Looking for experienced backend developer",
        "TasksCount": 5,
        "TasksType": 1
    }
    """
    logger.info("Получен запрос на добавление новой вакансии")
    
    # Получаем подключение к базе данных
    conn = get_db_connection()
    if not conn:
        logger.error("Ошибка подключения к базе данных")
        return jsonify({'error': 'Ошибка подключения к базе данных'}), 500
    
    try:
        # Получаем JSON данные из запроса
        data = request.get_json()

        if not data:
            logger.error("JSON данные не предоставлены")
            return jsonify({'error': 'JSON данные не предоставлены'}), 400
        
        logger.debug(f"Полученные данные: {data}")
        
        # Проверяем обязательные поля
        required_fields = ['ID_Company', 'ID_InterviewType', 'Name', 'Description']
        for field in required_fields:
            if field not in data:
                logger.error(f"Отсутствует обязательное поле: {field}")
                return jsonify({'error': f'Отсутствует обязательное поле: {field}'}), 400
        
        # Извлекаем данные с опциональными полями имеющими значения по умолчанию
        id_company = data['ID_Company']
        id_interview_type = data['ID_InterviewType']
        name = data['Name']
        description = data['Description']
        
        # Опциональные поля со значениями по умолчанию
        tasks_count = data.get('TasksCount', 0)
        tasks_type = data.get('TasksType', 0)
        
        # Проверяем типы данных
        if not isinstance(id_company, int) or not isinstance(id_interview_type, int):
            logger.error("ID_Company и ID_InterviewType должны быть целыми числами")
            return jsonify({'error': 'ID_Company и ID_InterviewType должны быть целыми числами'}), 400
        
        if tasks_count is not None and not isinstance(tasks_count, int):
            logger.error("TasksCount должен быть целым числом")
            return jsonify({'error': 'TasksCount должен быть целым числом'}), 400
        
        if tasks_type is not None and not isinstance(tasks_type, int):
            logger.error("TasksType должен быть целым числом")
            return jsonify({'error': 'TasksType должен быть целым числом'}), 400
        
        # Вставляем в базу данных
        cursor = conn.cursor()
        query = """
        INSERT INTO public."Vacancies" 
        ("ID_Company", "ID_InterviewType", "Name", "Description", "TasksCount", "TasksType")
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING "ID"
        """
        
        cursor.execute(query, (id_company, id_interview_type, name, description, tasks_count, tasks_type))
        
        # Получаем ID новой вставленной строки
        new_id = cursor.fetchone()[0]
        
        # Генерируем хеш для новой вакансии
        vacancy_hash = generate_vacancy_hash(new_id)
        
        # Коммитим транзакцию
        conn.commit()
        
        logger.info(f"Успешно добавлена новая вакансия с ID: {new_id}, Хеш: {vacancy_hash}")
        
        return jsonify({
            'message': 'Вакансия успешно добавлена',
            'id': new_id,
            'hash': vacancy_hash,
            'url': f'/vacancy/{vacancy_hash}'
        }), 201
        
    except psycopg2.Error as e:
        conn.rollback()
        logger.error(f"Ошибка базы данных: {e}")
        return jsonify({'error': f'Ошибка базы данных: {str(e)}'}), 500
    except Exception as e:
        conn.rollback()
        logger.error(f"Неожиданная ошибка: {e}")
        return jsonify({'error': f'Неожиданная ошибка: {str(e)}'}), 500
    finally:
        if conn:
            conn.close()
            logger.debug("Подключение к базе данных закрыто")

@app.route('/api/get-task', methods=['POST'])
def get_task():
    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Ошибка подключения к базе данных'}), 500
    
    data = request.get_json()

    print("Получены данные для получения задания:", data)
        
    if not data:
        return jsonify({'error': 'JSON данные не предоставлены'}), 400
    
    session_id = data.get('session_id', '')
        
    if not session_id:
        return jsonify({'error': 'Требуется ID сессии'}), 400
    
    # Получаем сессию
    with session_lock:
        if session_id not in chat_sessions:
            return jsonify({'error': 'Сессия не найдена'}), 404
        session = chat_sessions[session_id]
        
    difficulty = session.vacancy_data['Difficulty']

    # TODO: Переделать это, не хардкодить
    if difficulty == 'Junior':
        difficulty = 1

    cursor = conn.cursor()
    
    # Получаем все задания соответствующей сложности
    cursor.execute(f'SELECT "ID", "Name", "Text", "Tests" FROM public."Tasks" WHERE "ID_Difficulty" = {difficulty}')
    
    columns = [desc[0] for desc in cursor.description]
    
    ### TODO брать из бд
    testCount = 2
    testMetadata = 0
    ### TODO

    tasks = []
    for row in cursor.fetchall():
        task = dict(zip(columns, row))
        tasks.append(task)
    random_element = random.choice(tasks)
    session.code_task_data = random_element
    print("Выбрано случайное задание:", random_element['Name'])
    return jsonify({
    'taskName': random_element['Name'],
    'taskText': random_element['Text'],
    'testCount': testCount,
    'testMetadata': testMetadata,
    'timestamp': time.time()
}), 201
          

@app.route('/vacancies/<string:vacancy_hash>', methods=['GET'])
def get_vacancy_by_hash(vacancy_hash):
    """
    Получает данные вакансии по ее 8-символьному хешу
    Пример: /vacancies/qw93nfeq
    """
    logger.info(f"Получен запрос на получение вакансии с хешем: {vacancy_hash}")
    
    if len(vacancy_hash) != 8:
        logger.error(f"Неверная длина хеша: {len(vacancy_hash)}")
        return jsonify({'error': 'Неверный формат хеша вакансии'}), 400
    
    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Ошибка подключения к базе данных'}), 500
    
    try:
        cursor = conn.cursor()
        
        # Получаем все вакансии для поиска совпадающего хеша
        cursor.execute('SELECT "ID", "ID_Company", "ID_InterviewType", "Name", "Description", "TasksCount", "TasksType" FROM public."Vacancies"')
        
        columns = [desc[0] for desc in cursor.description]
        
        for row in cursor.fetchall():
            vacancy = dict(zip(columns, row))
            # Генерируем хеш для этого ID вакансии
            current_hash = generate_vacancy_hash(vacancy['ID'])
            
            # Если хеш совпадает, возвращаем эту вакансию
            if current_hash == vacancy_hash:
                logger.info(f"Найдена вакансия ID {vacancy['ID']} для хеша {vacancy_hash}")
                return jsonify(vacancy), 200
        
        # Если вакансия с таким хешем не найдена
        logger.warning(f"Вакансия с хешем не найдена: {vacancy_hash}")
        return jsonify({'error': 'Вакансия не найдена'}), 404
        
    except Exception as e:
        logger.error(f"Ошибка получения вакансии: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/vacancies', methods=['GET'])
def get_vacancies():
    """Получает все вакансии из базы данных с их хешами"""
    logger.info("Получен запрос на получение всех вакансий")
    
    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Ошибка подключения к базе данных'}), 500
    
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM public."Vacancies" ORDER BY "ID"')
        
        columns = [desc[0] for desc in cursor.description]
        vacancies = []
        
        for row in cursor.fetchall():
            vacancy = dict(zip(columns, row))
            # Добавляем хеш к каждой вакансии
            vacancy['hash'] = generate_vacancy_hash(vacancy['ID'])
            vacancy['url'] = f'/vacancy/{vacancy["hash"]}'
            vacancies.append(vacancy)
        
        logger.info(f"Получено {len(vacancies)} вакансий")
        return jsonify(vacancies), 200
        
    except Exception as e:
        logger.error(f"Ошибка получения вакансий: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/interview/<string:session_id>')
def interview_page(session_id):
    """
    Рендерит страницу чата собеседования
    """
    logger.info(f"Рендеринг страницы собеседования для сессии: {session_id}")
    
    # Проверяем существование сессии
    with session_lock:
        if session_id not in chat_sessions:
            return render_template('interview_not_found.html', 
                                 error='Сессия собеседования не найдена или истекла'), 404
        
        session = chat_sessions[session_id]
    
    return render_template('interview.html', 
                         session_id=session_id,
                         vacancy=session.vacancy_data,
                         applicant=session.application_data,
                         initial_message=session.messages[-1]['content'] if len(session.messages) > 1 else "Hello! Let's start the interview.")

@app.route('/vacancies/<int:vacancy_id>/hash', methods=['GET'])
def get_vacancy_hash(vacancy_id):
    """Получает хеш для конкретного ID вакансии"""
    logger.info(f"Получен запрос на получение хеша для ID вакансии: {vacancy_id}")
    
    hash_value = generate_vacancy_hash(vacancy_id)
    
    return jsonify({
        'id': vacancy_id,
        'hash': hash_value,
        'url': f'/vacancy/{hash_value}'
    }), 200


@app.route('/api/execute-python', methods=['POST'])
def execute_python():
    """
    Выполняет Python код в Docker контейнере и возвращает результат
    Ожидаемый JSON payload:
    {
        "code": "print('Hello World')"
    }
    """
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({'error': 'JSON данные не предоставлены'}), 400
        
        code = data.get('code', '').strip()
        
        if not code:
            return jsonify({'error': 'Код не предоставлен'}), 400
        
        if len(code) > 10000:  # лимит 10KB
            return jsonify({'error': 'Код слишком длинный. Максимум 10000 символов.'}), 400
        
        logger.info(f"Выполнение Python кода (длина: {len(code)} символов)")
    
        result = code_runner.run_in_docker_sdk(code)
        
    
        logger.info(f"Выполнение кода завершено. Код выхода: {result.get('exit_code')}, "
                   f"Время выполнения: {result.get('execution_time_seconds', 0):.2f}с")

        session_id = data.get('session_id', '')
        print("ID сессии для выполнения кода:", session_id)
        
        if not session_id:
            return jsonify({'error': 'Требуется ID сессии'}), 400
        
        # Получаем сессию
        with session_lock:
            if session_id not in chat_sessions:
                return jsonify({'error': 'Сессия не найдена'}), 404
            session = chat_sessions[session_id]
        
        changeQuestion = False
        NextQuestionType = -1
        isDone = False
        print('````````````````')
        print('ID сессии:', session_id, 'Текущий вопрос:', session.current_question, 'Всего вопросов:', session.question_count)
        if (result.get('exit_code') == 0):
            changeQuestion = True
            session.current_question += 1
            if session.current_question == session.question_count:
                isDone = True
        if session.current_question %2 == 0:
            NextQuestionType = 0
        else:
            NextQuestionType = 1
        print('Смена вопроса:', changeQuestion, 'Тип след. вопроса:', NextQuestionType, 'Завершено:', isDone)
        return jsonify({
            'success': result.get('exit_code', -1) == 0,
            'result': result,
            'timestamp': time.time(),
            'changeQuestion': changeQuestion,
            'NextQuestionType': NextQuestionType,
            'isDone': isDone
        }), 200
        
    except Exception as e:
        logger.error(f"Ошибка выполнения Python кода: {str(e)}")
        return jsonify({'error': f'Выполнение кода не удалось: {str(e)}'}), 500

# Фоновая thread для очистки
def cleanup_thread():
    while True:
        time.sleep(1800)  # Запускаем каждые 30 минут
        cleanup_inactive_sessions()

# Запускаем cleanup thread
cleanup_thread = threading.Thread(target=cleanup_thread, daemon=True)
cleanup_thread.start()

# Для запуска локально
if __name__ == '__main__':
    logger.info("Запуск Flask приложения на http://0.0.0.0:5002")
    app.run(host='0.0.0.0', port=5002, debug=True)