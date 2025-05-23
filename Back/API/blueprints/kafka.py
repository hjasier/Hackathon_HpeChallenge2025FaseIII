from flask import Blueprint, request, jsonify, render_template
from confluent_kafka import Consumer, KafkaException
import threading
import json
import time
from datetime import datetime
from flask_cors import cross_origin
import socket
import uuid
import psycopg2
from collections import deque
import logging
from threading import Lock

# Create a Blueprint instead of a Flask app
kafka_bp = Blueprint('kafka', __name__, url_prefix='/api/greenlake-eval/sensors')

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Thread-safe message store
class MessageStore:
    def __init__(self, max_size=10000000):
        self.messages = deque(maxlen=max_size)
        self.lock = Lock()
        self.sensor_city_map = {}  # Cache for sensor_id to city_id mapping
    
    def add_message(self, message):
        with self.lock:
            self.messages.append(message)
    
    def get_all(self):
        with self.lock:
            return list(self.messages)
    
    def get_by_city(self, city_id):
        with self.lock:
            return [msg for msg in self.messages if msg.get('city_id') == city_id]
    
    def get_by_sensor(self, sensor_id):
        with self.lock:
            return [msg for msg in self.messages if msg.get('sensor_id') == sensor_id]
    
    def cache_city(self, sensor_id, city_id):
        with self.lock:
            self.sensor_city_map[sensor_id] = city_id
    
    def get_cached_city(self, sensor_id):
        with self.lock:
            return self.sensor_city_map.get(sensor_id)

# Create message store
message_store = MessageStore()

# Kafka Configuration with unique consumer group ID
def get_kafka_config():
    # Generate a unique consumer group ID by combining hostname and a random UUID
    hostname = socket.gethostname()
    unique_id = str(uuid.uuid4())[:8]
    timestamp = int(time.time())
    group_id = f'sensor_metrics_consumer_{hostname}_{timestamp}_{unique_id}'
    
    #logger.info(f"Creating Kafka consumer with group_id: {group_id}")
    
    return {
        'bootstrap.servers': '10.10.76.231:7676',
        'group.id': group_id,
        'auto.offset.reset': 'earliest',
        'enable.auto.commit': True,
        'session.timeout.ms': 6000,
        'max.poll.interval.ms': 600000  # 10 minutes
    }

# Database Configuration
db_config = {
    'host': '10.10.76.241',
    'port': '6565',
    'database': 'greenlake_data',  # Update with actual database name
    'user': 'readonly_user',            # Update with actual username
    'password': 'asdf'         # Update with actual password
}

# Function to get database connection
def get_db_connection():
    try:
        #logger.info(f"Attempting to connect to database at {db_config['host']}:{db_config['port']}")
        conn = psycopg2.connect(**db_config)
        #logger.info("Database connection successful")
        return conn
    except Exception as e:
        logger.error(f"Database connection error: {e}")
        logger.error(f"Connection details: host={db_config['host']}, port={db_config['port']}, user={db_config['user']}, db={db_config['database']}")
        return None
def get_city_id_from_name(city_name):
    # Check cache first
    city_id = message_store.get_cached_city(city_name)
    if city_id is not None:
        return city_id
    
    # Query database
    conn = get_db_connection()
    if not conn:
        logger.error(f"Cannot get city_id for city {city_name}: Database connection failed")
        return None
    
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM cities WHERE name = %s", (city_name,))
        result = cursor.fetchone()
        
        if result:
            city_id = result[0]
            # Only cache valid city_ids
            if city_id is not None:
                message_store.cache_city(city_name, city_id)
            return city_id
        #logger.warning(f"No city_id found for city {city_name}")
        return None
    except Exception as e:
        logger.error(f"Database query error when getting city_id for city {city_name}: {e}")
        return None
    finally:
        cursor.close()
        conn.close()
# Function to get city_id for a sensor_id
def get_city_id(sensor_id):
    # Check cache first
    city_id = message_store.get_cached_city(sensor_id)
    if city_id is not None:
        return city_id
    
    # Query database
    conn = get_db_connection()
    if not conn:
        logger.error(f"Cannot get city_id for sensor {sensor_id}: Database connection failed")
        return None
    
    cursor = conn.cursor()
    try:
        # First check if sensor has direct city_id
        cursor.execute("SELECT city_id, road_id FROM sensors WHERE id = %s", (sensor_id,))
        result = cursor.fetchone()
        
        if not result:
            logger.warning(f"No sensor found with id {sensor_id}")
            return None
            
        city_id, road_id = result
        
        # If city_id is directly available, use it
        if city_id is not None:
            message_store.cache_city(sensor_id, city_id)
            return city_id
        
        # If no city_id but has road_id, get origin_city_id from roads table
        if road_id is not None:
            cursor.execute("SELECT origin_city_id FROM roads WHERE id = %s", (road_id,))
            road_result = cursor.fetchone()
            
            if road_result and road_result[0] is not None:
                city_id = road_result[0]
                # Cache and return the city_id from the road's origin city
                message_store.cache_city(sensor_id, city_id)
                return city_id
            
        logger.warning(f"No city_id found for sensor {sensor_id} (neither direct nor via road)")
        return None
    except Exception as e:
        logger.error(f"Database query error when getting city_id for sensor {sensor_id}: {e}")
        return None
    finally:
        cursor.close()
        conn.close()

# Function to consume Kafka messages
def consume_kafka_messages():
    consumer = Consumer(get_kafka_config())
    # Subscribe to all sensor topics including water usage
    consumer.subscribe(['sensor_metrics_air', 'sensor_metrics_ambient', 'sensor_metrics_traffic', 
                      'sensor_metrics_water_quality', 'sensor_metrics_water_usage'])
    
    try:
        while True:
            msg = consumer.poll(1.0)
            if msg is None:
                logger.debug("No message received")
                continue
            if msg.error():
                logger.error(f"Kafka error: {msg.error()}")
                continue
            
            try:
                # Parse message
                message_value = msg.value().decode('utf-8')
                data = json.loads(message_value)
                
                # Get topic name
                topic = msg.topic()
                
                # Add topic information to message
                data['source_topic'] = topic
                
                # Determine sensor type based on topic
                if topic == 'sensor_metrics_air':
                    data['sensor_type'] = 'air'
                elif topic == 'sensor_metrics_ambient':
                    data['sensor_type'] = 'ambient'
                elif topic == 'sensor_metrics_traffic':
                    data['sensor_type'] = 'traffic'
                elif topic == 'sensor_metrics_water_quality':
                    data['sensor_type'] = 'water_quality'
                elif topic == 'sensor_metrics_water_usage':
                    data['sensor_type'] = 'water_usage'
                
                # Get city_id for this sensor
                sensor_id = data.get('sensor_id')
                city_id = get_city_id(sensor_id)
                
                # Add city_id to the data
                data['city_id'] = city_id
                
                # Store message
                message_store.add_message(data)
                #logger.info(f"Processed message from {topic}: sensor_id={sensor_id}, city_id={city_id}")
                
            except Exception as e:
                logger.error(f"Error processing message: {e}")
    
    except Exception as e:
        logger.error(f"Consumer error: {e}")
    finally:
        consumer.close()

# Start the Kafka consumer thread when the blueprint is registered
consumer_thread = None

@kafka_bp.record
def on_register(state):
    logger.info("Registering Kafka consumer thread")
    global consumer_thread
    consumer_thread = threading.Thread(target=consume_kafka_messages)
    consumer_thread.daemon = True
    consumer_thread.start()
    #logger.info("Kafka consumer thread started")

# Route to get all sensor data with city information
@kafka_bp.route('/data', methods=['GET'])
@cross_origin()
def get_data():
    return jsonify(message_store.get_all())

@kafka_bp.route('/data/<city_name>', methods=['GET'])
@cross_origin()
def get_sensor_data_by_city(city_name):
    # Get all messages for the specified city
    city_id = get_city_id_from_name(city_name)
    if not city_id:
        return jsonify({'error': f'City not found: {city_name}'}), 404
    messages = message_store.get_by_city(city_id)
    
    if not messages:
        return jsonify({'error': f'No data found for city: {city_name}'}), 404
    
    # Return the filtered messages
    return jsonify(messages)
@kafka_bp.route('/cities', methods=['GET'])
@cross_origin()
def get_cities():
    # Get all unique city_ids from the messages and get names from the database
    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Database connection failed'}), 500
    cursor = conn.cursor()
    # check how many sensors are in each city
    #cursor.execute("SELECT city_id COUNT(*) FROM sensors GROUP BY city_id")
    #city_sensor_count = cursor.fetchall()
    cursor.execute("SELECT DISTINCT name FROM cities WHERE id IN (SELECT DISTINCT city_id FROM sensors)")
    cities = cursor.fetchall()
    cursor.close()
    conn.close()
    
    return jsonify(list(cities))

@kafka_bp.route('/sensor-data', methods=['GET'])
@cross_origin()
def get_sensor_data():
    sensor_id = request.args.get('sensor_id')
    if not sensor_id:
        return jsonify({'error': 'sensor_id is required'}), 400
    
    data = message_store.get_by_sensor(sensor_id)
    if not data:
        return jsonify({'error': 'No data found for this sensor'}), 404
    
    return jsonify(data)

# Route to get data for a specific city
@kafka_bp.route('/city/<city_id>', methods=['GET'])
@cross_origin()
def get_city_data(city_id):
    return jsonify(message_store.get_by_city(city_id))


# Health check endpoint
@kafka_bp.route('/health', methods=['GET'])
@cross_origin()
def health_check():
    return jsonify({
        'status': 'ok',
        'consumer_running': consumer_thread is not None and consumer_thread.is_alive(),
        'message_count': len(message_store.get_all()),
        'cache_size': len(message_store.sensor_city_map)
    })

@kafka_bp.route('/<operation>', methods=['GET'])
@cross_origin()
def aggregate_sensor_data(operation):
    # Add entry point logging
    #logger.info(f"=== ENDPOINT CALLED: /{operation} with params: {request.args} ===")
    
    # Validate operation type
    if operation not in ['average', 'min', 'max']:
        logger.warning(f"Invalid operation requested: {operation}")
        return jsonify({'error': f'Invalid operation: {operation}. Allowed values: average, min, max'}), 400
    
    # Get required query parameters
    city_id = request.args.get('city_id')
    sensor_type = request.args.get('sensor_type')
    date_str = request.args.get('date')
    
    # Log all parameters
    #logger.info(f"Parameters: city_id={city_id}, sensor_type={sensor_type}, date={date_str}")
    
    # Validate required parameters
    if not city_id:
        logger.warning("Missing required parameter: city_id")
        return jsonify({'error': 'city_id parameter is required'}), 400
    if not sensor_type:
        logger.warning("Missing required parameter: sensor_type")
        return jsonify({'error': 'sensor_type parameter is required'}), 400
    if not date_str:
        logger.warning("Missing required parameter: date")
        return jsonify({'error': 'date parameter is required'}), 400
    
    # Validate sensor_type
    valid_sensor_types = ['air', 'ambient', 'traffic', 'water_quality', 'water_usage']
    if sensor_type not in valid_sensor_types:
        logger.warning(f"Invalid sensor_type: {sensor_type}")
        return jsonify({'error': f'Invalid sensor_type: {sensor_type}. Allowed values: {", ".join(valid_sensor_types)}'}), 400
    
    try:
        # Parse the date string
        target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        #logger.info(f"Parsed date: {target_date}")
    except ValueError:
        logger.warning(f"Invalid date format: {date_str}")
        return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
    
    # Get all messages for the specified city
    messages = message_store.get_by_city(city_id)
    # Log if we found messages with null city_id
    if not messages:
        logger.warning(f"No messages found for city_id={city_id}")
    else:
        null_city_messages = [msg for msg in messages if msg.get('city_id') is None]
        if null_city_messages:
            logger.warning(f"Found {len(null_city_messages)} messages with null city_id for request city_id={city_id}")
    
    # Filter by sensor type and date
    filtered_messages = []
    for msg in messages:
        # Skip messages without event_time
        if 'event_time' not in msg:
            logger.warning(f"Message without event_time: {msg}")
            continue
        
        # Parse event_time and compare only the date part
        try:
            event_time = datetime.fromisoformat(msg['event_time'].replace('Z', '+00:00'))
            #logger.info(f"Event time: {event_time}")
            event_date = event_time.date()
            
            # Check if this message is from the target date and matches the requested sensor type
            if event_date == target_date:
                # For air sensor type
                if sensor_type == 'air' and msg.get('sensor_type') == 'air':
                    filtered_messages.append(msg)
                # For ambient sensor type
                elif sensor_type == 'ambient' and msg.get('sensor_type') == 'ambient':
                    filtered_messages.append(msg)
                # For traffic sensor type
                elif sensor_type == 'traffic' and msg.get('sensor_type') == 'traffic':
                    filtered_messages.append(msg)
                # For water quality sensor type
                elif sensor_type == 'water_quality' and msg.get('sensor_type') == 'water_quality':
                    filtered_messages.append(msg)
                # For water usage sensor type
                elif sensor_type == 'water_usage' and msg.get('sensor_type') == 'water_usage':
                    filtered_messages.append(msg)
        except (ValueError, TypeError):
            # Skip messages with invalid timestamps
            continue
    
    # If no data was found
    if not filtered_messages:
        return jsonify({'error': f'No data found for city_id={city_id}, sensor_type={sensor_type}, date={date_str}'}), 404
    
    # Calculate metrics based on sensor type
    metrics_list = []
    
    # Define units for each metric type
    metric_units = {
        'air': {
            'pm10': 'µg/m³',
            'co': 'ppm',
            'co2': 'ppm',
            'no2': 'ppb',
            'o3': 'ppb',
            'so2': 'ppb'
        },
        'ambient': {
            'humidity': '%',
            'temperature': '°C',
            'solar_radiation': 'W/m²'
        },
        'traffic': {
            'avg_speed': 'km/h',
            'flow_rate': 'vehicles/hour',
            'occupancy': '%',
            'vehicle_density': 'vehicles/km',
            'congestion_index': 'index'
        },
        'water_quality': {
            'ph_level': 'pH',
            'turbidity': 'NTU',
            'conductivity': 'µS/cm',
            'dissolved_oxygen': 'mg/L',
            'water_temperature': '°C'
        },
        'water_usage': {
            'usage_liters': 'L',
            'total_daily_usage': 'L'
        }
    }
    
    if sensor_type == 'air':
        # List of air metrics to aggregate
        air_metrics = ['co', 'o3', 'co2', 'no2', 'so2', 'pm10']
        
        for metric in air_metrics:
            # Collect all values for this metric
            values = [float(msg.get(metric, 0)) for msg in filtered_messages if msg.get(metric) is not None]
            
            if values:
                if operation == 'average':
                    value = sum(values) / len(values)
                elif operation == 'min':
                    value = min(values)
                elif operation == 'max':
                    value = max(values)
                
                metrics_list.append({
                    'metric': metric,
                    'unit': metric_units['air'].get(metric, ''),
                    'value': round(value, 2)
                })
    
    elif sensor_type == 'ambient':
        # List of ambient metrics to aggregate
        ambient_metrics = ['humidity', 'temperature', 'solar_radiation']
        
        for metric in ambient_metrics:
            # Collect all values for this metric
            values = [float(msg.get(metric, 0)) for msg in filtered_messages if msg.get(metric) is not None]
            
            if values:
                if operation == 'average':
                    value = sum(values) / len(values)
                elif operation == 'min':
                    value = min(values)
                elif operation == 'max':
                    value = max(values)
                
                metrics_list.append({
                    'metric': metric,
                    'unit': metric_units['ambient'].get(metric, ''),
                    'value': round(value, 2)
                })
    
    elif sensor_type == 'traffic':
        # List of traffic metrics to aggregate
        traffic_metrics = ['avg_speed', 'flow_rate', 'occupancy', 'vehicle_density', 'congestion_index']
        
        for metric in traffic_metrics:
            # Collect all values for this metric
            values = [float(msg.get(metric, 0)) for msg in filtered_messages if msg.get(metric) is not None]
            
            if values:
                if operation == 'average':
                    value = sum(values) / len(values)
                elif operation == 'min':
                    value = min(values)
                elif operation == 'max':
                    value = max(values)
                
                metrics_list.append({
                    'metric': metric,
                    'unit': metric_units['traffic'].get(metric, ''),
                    'value': round(value, 2)
                })
    
    elif sensor_type == 'water_quality':
        # List of water quality metrics to aggregate
        water_quality_metrics = ['ph_level', 'turbidity', 'conductivity', 'dissolved_oxygen', 'water_temperature']
        
        for metric in water_quality_metrics:
            # Collect all values for this metric
            values = [float(msg.get(metric, 0)) for msg in filtered_messages if msg.get(metric) is not None]
            
            if values:
                if operation == 'average':
                    value = sum(values) / len(values)
                elif operation == 'min':
                    value = min(values)
                elif operation == 'max':
                    value = max(values)
                
                metrics_list.append({
                    'metric': metric,
                    'unit': metric_units['water_quality'].get(metric, ''),
                    'value': round(value, 2)
                })
    
    elif sensor_type == 'water_usage':
        # List of water usage metrics to aggregate
        water_usage_metrics = ['usage_liters']
        
        for metric in water_usage_metrics:
            # Collect all values for this metric
            values = [float(msg.get(metric, 0)) for msg in filtered_messages if msg.get(metric) is not None]
            
            if values:
                if operation == 'average':
                    value = sum(values) / len(values)
                elif operation == 'min':
                    value = min(values)
                elif operation == 'max':
                    value = max(values)
                
                metrics_list.append({
                    'metric': metric,
                    'unit': metric_units['water_usage'].get(metric, ''),
                    'value': round(value, 2)
                })
                
                # Add daily total for water usage
                if metric == 'usage_liters':
                    metrics_list.append({
                        'metric': 'total_daily_usage',
                        'unit': metric_units['water_usage'].get('total_daily_usage', ''),
                        'value': round(sum(values), 2)
                    })
    
    # Get current timestamp in ISO format
    current_timestamp = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    
    # Prepare the response
    response = {
        'metadata': {
            'status': 'success',
            'timestamp': current_timestamp
        },
        'results': metrics_list
    }
    
    return jsonify(response)

