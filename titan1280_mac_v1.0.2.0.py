import cv2
import numpy as np
from datetime import datetime
import json
import os
import threading

# Try to import serial, provide fallback if not available
try:
    import serial
    import serial.tools.list_ports
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False
    print("Warning: pyserial not installed. Serial features disabled. Install with: pip install pyserial")

# ---- enum ----

# May need to change cam index
cam_index = 0	
last_serial_port = None      # e.g. "COM3" or "/dev/ttyUSB0"
last_baud_rate = 115200
SETTINGS_FILE = "titan1280_settings.json"

# Video recording
recording = False
video_writer = None
record_start_time = None
VIDEO_FOURCC = cv2.VideoWriter_fourcc(*'H264')   # or *'avc1' or *'x264' or 'H264'
VIDEO_FPS = 7.5                                 # adjust to your actual frame rate

# ---- color palettes for false color ----
def create_palettes():
    """Create various false color palettes (256 entries each)"""
    palettes = {}
    lut = np.arange(256, dtype=np.uint8).reshape(256, 1)
    
    # Grayscale (default)
    gray = np.zeros((256, 1, 3), dtype=np.uint8)
    for i in range(256):
        gray[i, 0] = [i, i, i]
    palettes['grayscale'] = gray
    palettes['jet'] = cv2.applyColorMap(lut, cv2.COLORMAP_JET)
    palettes['hot'] = cv2.applyColorMap(lut, cv2.COLORMAP_HOT)
    palettes['bone'] = cv2.applyColorMap(lut, cv2.COLORMAP_BONE)
    palettes['inferno'] = cv2.applyColorMap(lut, cv2.COLORMAP_INFERNO)
    palettes['viridis'] = cv2.applyColorMap(lut, cv2.COLORMAP_VIRIDIS)
    palettes['plasma'] = cv2.applyColorMap(lut, cv2.COLORMAP_PLASMA)
    palettes['turbo'] = cv2.applyColorMap(lut, cv2.COLORMAP_TURBO)
    palettes['rainbow'] = cv2.applyColorMap(lut, cv2.COLORMAP_RAINBOW)
    palettes['ocean'] = cv2.applyColorMap(lut, cv2.COLORMAP_OCEAN)
    
    return palettes

palettes = create_palettes()
palette_names = list(palettes.keys())
current_palette_index = 0
invert_palette = False  # Toggle for inverting the palette

# ---- Offset/Range controls ----
auto_range = True  # Auto-range enabled by default
manual_offset = 0  # 0-65535 for 16-bit
manual_range = 65535  # 1-65535 for 16-bit
show_histogram = True  # Toggle for histogram display
show_cursor_readout = True  # Toggle for pixel readout display

# ---- Auto-range percentile settings (adjustable) ----
AUTO_RANGE_LOW_PERCENTILE = 0.0   # Clip lowest X% of pixels
AUTO_RANGE_HIGH_PERCENTILE = 100.0  # Clip highest X% of pixels

# ---- Sharpening options (using Unsharp Mask - better for thermal) ----
sharpen_levels = ['Off', 'Low', 'Medium', 'High']
sharpen_index = 0  # Default: Off

# Unsharp mask parameters: (blur_size, strength)
# Uses Gaussian blur + weighted blend for clean edge enhancement
sharpen_params = {
    'Off': None,
    'Low': (5, 0.5),      # Subtle edge enhancement
    'Medium': (5, 1.0),    # Moderate sharpening
    'High': (7, 1.5),      # Strong sharpening
}

# ---- Serial port settings ----
BAUD_RATES = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]
selected_baud_index = 4  # Default: 115200 (index 4)
serial_port = None
serial_connection = None
available_ports = []
selected_port_index = 0
serial_thread = None
serial_running = False
serial_rx_buffer = []  # Buffer for received messages
serial_tx_buffer = ""  # Current text being typed
serial_input_active = False  # Whether text input is focused


# Line ending for serial transmission (modify as needed)
SERIAL_LINE_ENDING = ""  # Options: "", "\n", "\r", "\r\n"

def scan_serial_ports():
    """Scan for available serial ports"""
    global available_ports
    if not SERIAL_AVAILABLE:
        available_ports = []
        return
    ports = serial.tools.list_ports.comports()
    available_ports = [p.device for p in ports]
    if not available_ports:
        available_ports = ["No ports"]

def connect_serial(port_name):
    """Connect to a serial port"""
    global serial_connection, serial_thread, serial_running
    if not SERIAL_AVAILABLE or port_name == "No ports":
        return False
    
    baud = BAUD_RATES[selected_baud_index]
    try:
        if serial_connection and serial_connection.is_open:
            disconnect_serial()
        
        serial_connection = serial.Serial(port_name, baud, timeout=0.1)
        serial_running = True
        serial_thread = threading.Thread(target=serial_read_thread, daemon=True)
        serial_thread.start()
        print(f"Connected to {port_name} at {baud} baud")
        return True
    except Exception as e:
        print(f"Failed to connect to {port_name}: {e}")
        return False

def disconnect_serial():
    """Disconnect from serial port"""
    global serial_connection, serial_running
    serial_running = False
    if serial_connection:
        try:
            serial_connection.close()
        except:
            pass
        serial_connection = None
        print("Serial disconnected")

def serial_read_thread():
    global serial_rx_buffer
    while serial_running and serial_connection and serial_connection.is_open:
        try:
            if serial_connection.in_waiting > 0:
                # Read all available bytes (or up to a reasonable chunk)
                data = serial_connection.read(serial_connection.in_waiting)
                # Store as hex string for display
                hex_str = data.hex().upper()
                serial_rx_buffer.append(hex_str)
                if len(serial_rx_buffer) > 10:
                    serial_rx_buffer.pop(0)
                print(f"Serial RX (hex): {hex_str}")
                # Optional: also log raw bytes if you want
                # print(f"Serial RX (raw): {data}")
        except Exception as e:
            print(f"Serial read error: {e}")
            break

def serial_send(text):
    """Send hex string as bytes over serial"""
    global serial_connection
    if serial_connection and serial_connection.is_open:
        try:
            # Parse space-separated hex (e.g., "AA 04 ..." -> bytes)
            hex_bytes = bytes.fromhex(text)
            serial_connection.write(hex_bytes)
            print(f"Serial TX (hex): {text}")
            return True
        except ValueError as e:
            print(f"Invalid hex format: {e}")
        except Exception as e:
            print(f"Serial send error: {e}")
    return False

# Initial port scan
scan_serial_ports()


# ---- Settings persistence ----
def load_settings():
    """Load settings from JSON file"""
    global current_palette_index, invert_palette, auto_range, manual_offset, manual_range
    global show_histogram, sharpen_index, show_cursor_readout
    global last_serial_port, last_baud_rate, selected_port_index, selected_baud_index

    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r') as f:
                settings = json.load(f)
                
                current_palette_index = settings.get('palette_index', 0)
                invert_palette        = settings.get('invert', False)
                auto_range            = settings.get('auto_range', True)
                manual_offset         = settings.get('offset', 0)
                manual_range          = settings.get('range', 65535)
                show_histogram        = settings.get('show_histogram', True)
                sharpen_index         = settings.get('sharpen_index', 0)
                show_cursor_readout   = settings.get('show_cursor_readout', True)
                
                # ── New serial settings ──
                last_serial_port = settings.get('last_serial_port', None)
                last_baud         = settings.get('last_baud_rate', 115200)
                
                # Try to restore selection indices if ports are available
                if last_serial_port and SERIAL_AVAILABLE:
                    scan_serial_ports()  # make sure list is fresh
                    if last_serial_port in available_ports:
                        selected_port_index = available_ports.index(last_serial_port)
                        # Try to auto-reconnect to last used port if it still exists
                        connect_serial(last_serial_port)
                    else:
                        print(f"Previously used port {last_serial_port} not found")
                
                # Find baud index
                if last_baud in BAUD_RATES:
                    selected_baud_index = BAUD_RATES.index(last_baud)
                else:
                    selected_baud_index = 4  # fallback to 115200
                
                print(f"Settings loaded from {SETTINGS_FILE}")
        except Exception as e:
            print(f"Could not load settings: {e}")

def save_settings():
    """Save current settings to JSON file"""
    global last_serial_port, last_baud_rate
    
    # Update "last used" values before saving
    if available_ports and selected_port_index < len(available_ports):
        last_serial_port = available_ports[selected_port_index]
    else:
        last_serial_port = None
        
    last_baud_rate = BAUD_RATES[selected_baud_index]
    
    settings = {
        'palette_index':       current_palette_index,
        'invert':              invert_palette,
        'auto_range':          auto_range,
        'offset':              manual_offset,
        'range':               manual_range,
        'show_histogram':      show_histogram,
        'sharpen_index':       sharpen_index,
        'show_cursor_readout': show_cursor_readout,
        'last_serial_port':    last_serial_port,
        'last_baud_rate':      last_baud_rate,
    }
    
    try:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(settings, f, indent=2)
        print(f"Settings saved to {SETTINGS_FILE}")
    except Exception as e:
        print(f"Could not save settings: {e}")

# Load settings at startup
load_settings()

# ---- button definitions ----
button_height = 30
button_width = 100
button_margin = 10
buttons = []

# ---- slider definitions ----
slider_width = 200
slider_height = 20
slider_track_height = 6
sliders = []
dragging_slider = None  # Track which slider is being dragged

# ---- serial definitions ----
SERIAL_INPUT_WIDTH = 300
SERIAL_INPUT_HEIGHT = 25

def create_buttons():
    """Create button definitions"""
    global buttons
    row2_y = button_margin + button_height + button_margin  # Second row for serial controls
    
    buttons = [
        # Row 1: Image controls
        {'name': 'Save TIF', 'x': button_margin, 'y': button_margin, 'w': button_width, 'h': button_height, 'action': 'save_tif'},
        {'name': 'Save PNG', 'x': button_margin + button_width + button_margin, 'y': button_margin, 'w': button_width, 'h': button_height, 'action': 'save_png'},
        {'name': 'Palette', 'x': button_margin + 2 * (button_width + button_margin), 'y': button_margin, 'w': button_width, 'h': button_height, 'action': 'cycle_palette'},
        {'name': 'Invert', 'x': button_margin + 3 * (button_width + button_margin), 'y': button_margin, 'w': button_width, 'h': button_height, 'action': 'toggle_invert'},
        {'name': 'Auto Range', 'x': button_margin + 4 * (button_width + button_margin), 'y': button_margin, 'w': button_width, 'h': button_height, 'action': 'toggle_autorange'},
        {'name': 'Histogram', 'x': button_margin + 5 * (button_width + button_margin), 'y': button_margin, 'w': button_width, 'h': button_height, 'action': 'toggle_histogram'},
        {'name': 'Sharpen', 'x': button_margin + 6 * (button_width + button_margin), 'y': button_margin, 'w': button_width, 'h': button_height, 'action': 'cycle_sharpen'},
        {'name': 'Cursor Info', 'x': button_margin + 7 * (button_width + button_margin), 'y': button_margin, 'w': button_width, 'h': button_height, 'action': 'show_cursor_readout'},

        # recording button
        {'name': 'Record', 'x': button_margin + 8 * (button_width + button_margin), 'y': button_margin, 'w': button_width, 'h': button_height, 'action': 'toggle_record'},

        # Row 2: Serial controls
        {'name': 'Port',   'x': button_margin,                                      'y': row2_y, 'w': button_width,     'h': button_height, 'action': 'cycle_port'},
        {'name': 'Baud',   'x': button_margin + button_width + button_margin,       'y': row2_y, 'w': 80,                  'h': button_height, 'action': 'cycle_baud'},
        {'name': 'Connect','x': button_margin + button_width + button_margin + 80 + button_margin, 'y': row2_y, 'w': button_width, 'h': button_height, 'action': 'toggle_serial'},
        {'name': 'Send',   'x': button_margin + button_width + button_margin + 80 + button_margin + button_width + button_margin, 'y': row2_y, 'w': 60, 'h': button_height, 'action': 'serial_send'},
        
        # ── NUC buttons ──
        {'name': 'NUC', 
         'x': button_margin + button_width + button_margin + 80 + button_margin + button_width + button_margin + 60 + SERIAL_INPUT_WIDTH + button_margin + 10,
         'y': row2_y, 
         'w': 80, 
         'h': button_height, 
         'action': 'nuc_normal'},
        
        {'name': 'No Shutter NUC', 
         'x': button_margin + button_width + button_margin + 80 + button_margin + button_width + button_margin + 60 + SERIAL_INPUT_WIDTH + button_margin + 80 + button_margin + 10,
         'y': row2_y, 
         'w': 120, 
         'h': button_height, 
         'action': 'nuc_no_shutter'},
    ]

def create_sliders():
    """Create slider definitions"""
    global sliders
    slider_y_start = button_margin + button_height + button_margin + button_height + button_margin + 10
    sliders = [
        {'name': 'Offset', 'x': button_margin, 'y': slider_y_start, 'w': slider_width, 'h': slider_height, 'min': 0, 'max': 65535, 'value': 'manual_offset'},
        {'name': 'Range', 'x': button_margin + slider_width + 60, 'y': slider_y_start, 'w': slider_width, 'h': slider_height, 'min': 1, 'max': 65535, 'value': 'manual_range'},
    ]

create_buttons()
create_sliders()

# Global state
current_gray16 = None
current_preview = None  # Clean preview without UI elements (for PNG saving)
mouse_x, mouse_y = -1, -1  # Current mouse position

def update_slider_value(slider, x):
    """Update slider value based on x position"""
    global manual_offset, manual_range
    # Calculate relative position
    rel_x = max(0, min(x - slider['x'], slider['w']))
    ratio = rel_x / slider['w']
    value = int(slider['min'] + ratio * (slider['max'] - slider['min']))
    value = max(slider['min'], min(slider['max'], value))
    
    if slider['value'] == 'manual_offset':
        manual_offset = value
    elif slider['value'] == 'manual_range':
        manual_range = max(1, value)  # Ensure range is at least 1

def mouse_callback(event, x, y, flags, param):
    global current_palette_index, current_gray16, current_preview
    global invert_palette, auto_range, dragging_slider, show_histogram, sharpen_index
    global selected_port_index, selected_baud_index, serial_connection, serial_input_active, serial_tx_buffer
    global mouse_x, mouse_y, show_cursor_readout
    
    # Always track mouse position for pixel value display
    mouse_x, mouse_y = x, y
    
    if event == cv2.EVENT_LBUTTONDOWN:
        # Check if clicking on serial text input area
        serial_input_box = get_serial_input_box()
        if serial_input_box:
            if (serial_input_box['x'] <= x <= serial_input_box['x'] + serial_input_box['w'] and
                serial_input_box['y'] <= y <= serial_input_box['y'] + serial_input_box['h']):
                serial_input_active = True
                return
        serial_input_active = False
        
        # Check buttons first
        for btn in buttons:
            if btn['x'] <= x <= btn['x'] + btn['w'] and btn['y'] <= y <= btn['y'] + btn['h']:
                if btn['action'] == 'save_tif':
                    if current_gray16 is not None:
                        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                        filename = f"{timestamp}_thermal.tif"
                        cv2.imwrite(filename, current_gray16)
                        print(f"Saved raw 16-bit TIF: {filename}")
                elif btn['action'] == 'save_png':
                    if current_preview is not None:
                        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                        filename = f"{timestamp}_thermal.png"
                        cv2.imwrite(filename, current_preview)
                        print(f"Saved PNG: {filename}")
                elif btn['action'] == 'cycle_palette':
                    current_palette_index = (current_palette_index + 1) % len(palette_names)
                    print(f"Palette: {palette_names[current_palette_index]}")
                elif btn['action'] == 'toggle_invert':
                    invert_palette = not invert_palette
                    print(f"Invert: {'ON' if invert_palette else 'OFF'}")
                elif btn['action'] == 'toggle_autorange':
                    auto_range = not auto_range
                    print(f"Auto Range: {'ON' if auto_range else 'OFF'}")
                elif btn['action'] == 'toggle_histogram':
                    show_histogram = not show_histogram
                    print(f"Histogram: {'ON' if show_histogram else 'OFF'}")
                elif btn['action'] == 'cycle_sharpen':
                    sharpen_index = (sharpen_index + 1) % len(sharpen_levels)
                    print(f"Sharpen: {sharpen_levels[sharpen_index]}")   
                elif btn['action'] == 'show_cursor_readout':
                    show_cursor_readout = not show_cursor_readout
                    print(f"Cursor readout: {'ON' if show_cursor_readout else 'OFF'}")


                elif btn['action'] == 'cycle_port':
                    if available_ports:
                        selected_port_index = (selected_port_index + 1) % len(available_ports)
                        print(f"Selected port: {available_ports[selected_port_index]}")
                elif btn['action'] == 'cycle_baud':
                    selected_baud_index = (selected_baud_index + 1) % len(BAUD_RATES)
                    print(f"Selected baud: {BAUD_RATES[selected_baud_index]}")
                elif btn['action'] == 'toggle_serial':
                    if serial_connection and serial_connection.is_open:
                        disconnect_serial()
                    else:
                        if available_ports and available_ports[selected_port_index] != "No ports":
                            connect_serial(available_ports[selected_port_index])
                elif btn['action'] == 'serial_send':
                    if serial_tx_buffer:
                        serial_send(serial_tx_buffer)
                        serial_tx_buffer = ""
                elif btn['action'] == 'nuc_normal':
                    serial_send("AA 05 00 16 01 00 C6 EB AA")
                    print("Sent NUC command")

                elif btn['action'] == 'nuc_no_shutter':
                    serial_send("AA 05 00 16 01 02 C8 EB AA")
                    print("Sent No Shutter NUC command")
                

                elif btn['action'] == 'toggle_record':
                            global recording, video_writer, record_start_time
                            
                            if not recording:
                                # Start recording
                                timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                                filename = f"thermal_{timestamp}.mp4"
                                
                                h, w = current_preview.shape[:2]
                                video_writer = cv2.VideoWriter(
                                    filename,
                                    VIDEO_FOURCC,
                                    VIDEO_FPS,
                                    (w, h)
                                )
                                
                                if not video_writer.isOpened():
                                    print("Error: Could not open VideoWriter")
                                    return
                                
                                recording = True
                                record_start_time = datetime.now()
                                print(f"Recording started → {filename}")
                            else:
                                # Stop recording
                                if video_writer:
                                    video_writer.release()
                                    video_writer = None
                                recording = False
                                duration = (datetime.now() - record_start_time).total_seconds()
                                print(f"Recording stopped ({duration:.1f} s)")
                            return
        
        # Check sliders (only if auto_range is disabled)
        if not auto_range:
            for slider in sliders:
                if slider['x'] <= x <= slider['x'] + slider['w'] and slider['y'] <= y <= slider['y'] + slider['h']:
                    dragging_slider = slider
                    update_slider_value(slider, x)
                    return

        
    
    elif event == cv2.EVENT_MOUSEMOVE:
        if dragging_slider is not None and not auto_range:
            update_slider_value(dragging_slider, x)
    
    elif event == cv2.EVENT_LBUTTONUP:
        dragging_slider = None
    
    elif event == cv2.EVENT_RBUTTONDOWN:
        # Toggle cursor readout with right click
        show_cursor_readout = not show_cursor_readout
        print(f"Cursor readout: {'ON' if show_cursor_readout else 'OFF'}")

def draw_buttons(img):
    for btn in buttons:
        bg_color = (60, 60, 60)
        if btn['action'] == 'toggle_record' and recording:
            bg_color = (0, 0, 175)  # red when active
        elif btn['action'] == 'toggle_invert' and invert_palette:
            bg_color = (0, 100, 0)  # Green when active
        elif btn['action'] == 'toggle_autorange' and auto_range:
            bg_color = (0, 100, 0)  # Green when active
        elif btn['action'] == 'toggle_histogram' and show_histogram:
            bg_color = (0, 100, 0)  # Green when active
        elif btn['action'] == 'cycle_sharpen' and sharpen_index > 0:
            bg_color = (0, 100, 0)  # Green when active (any level except Off)
        elif btn['action'] == 'show_cursor_readout' and show_cursor_readout:
            bg_color = (0, 100, 0)  # Green when active 
        elif btn['action'] == 'toggle_serial' and serial_connection and serial_connection.is_open:
            bg_color = (0, 100, 0)  # Green when connected
        
        # Button background
        cv2.rectangle(img, (btn['x'], btn['y']), (btn['x'] + btn['w'], btn['y'] + btn['h']), bg_color, -1)
        # Button border
        cv2.rectangle(img, (btn['x'], btn['y']), (btn['x'] + btn['w'], btn['y'] + btn['h']), (120, 120, 120), 1)
        
        # Button text
        text = btn['name']
        if btn['action'] == 'toggle_record':
            text = 'Stop' if recording else 'Record'
        if btn['action'] == 'cycle_palette':
            text = palette_names[current_palette_index].capitalize()
        elif btn['action'] == 'toggle_invert':
            text = 'Invert ON' if invert_palette else 'Invert OFF'
        elif btn['action'] == 'toggle_autorange':
            text = 'Auto ON' if auto_range else 'Auto OFF'
        elif btn['action'] == 'toggle_histogram':
            text = 'Hist ON' if show_histogram else 'Hist OFF'
        elif btn['action'] == 'cycle_sharpen':
            text = f"Sharp: {sharpen_levels[sharpen_index]}"
        elif btn['action'] == 'cycle_port':
            if available_ports:
                port = available_ports[selected_port_index]
                # Shorten COM port name if needed
                text = port if len(port) <= 10 else port[-10:]
            else:
                text = "No ports"
        elif btn['action'] == 'cycle_baud':
            text = str(BAUD_RATES[selected_baud_index])
        elif btn['action'] == 'toggle_serial':
            text = 'Disconnect' if (serial_connection and serial_connection.is_open) else 'Connect'
        
        text_size = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.4, 1)[0]
        text_x = btn['x'] + (btn['w'] - text_size[0]) // 2
        text_y = btn['y'] + (btn['h'] + text_size[1]) // 2
        cv2.putText(img, text, (text_x, text_y), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)
    
    return img

def draw_sliders(img):
    """Draw sliders on the image (only visible when auto_range is off)"""
    if auto_range:
        return img
    
    for slider in sliders:
        # Get current value
        if slider['value'] == 'manual_offset':
            current_val = manual_offset
        else:
            current_val = manual_range
        
        # Calculate handle position
        ratio = (current_val - slider['min']) / (slider['max'] - slider['min'])
        handle_x = int(slider['x'] + ratio * slider['w'])
        
        # Draw slider label
        label = f"{slider['name']}: {current_val}"
        cv2.putText(img, label, (slider['x'], slider['y'] - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
        
        # Draw track background
        track_y = slider['y'] + (slider['h'] - slider_track_height) // 2
        cv2.rectangle(img, (slider['x'], track_y), (slider['x'] + slider['w'], track_y + slider_track_height), (40, 40, 40), -1)
        cv2.rectangle(img, (slider['x'], track_y), (slider['x'] + slider['w'], track_y + slider_track_height), (80, 80, 80), 1)
        
        # Draw filled portion
        cv2.rectangle(img, (slider['x'], track_y), (handle_x, track_y + slider_track_height), (100, 150, 100), -1)
        
        # Draw handle
        handle_radius = slider['h'] // 2
        cv2.circle(img, (handle_x, slider['y'] + slider['h'] // 2), handle_radius, (200, 200, 200), -1)
        cv2.circle(img, (handle_x, slider['y'] + slider['h'] // 2), handle_radius, (255, 255, 255), 1)
    
    return img

# ---- Serial UI settings ----

def get_serial_input_box():
    """Get the position and size of the serial input box"""
    # Position after the Send button
    row2_y = button_margin + button_height + button_margin 
    send_btn_end_x = button_margin + button_width + button_margin + 80 + button_margin + button_width + button_margin + 60 + button_margin
    return {
        'x': send_btn_end_x,
        'y': row2_y + 2,
        'w': SERIAL_INPUT_WIDTH,
        'h': SERIAL_INPUT_HEIGHT
    }

def draw_serial_ui(img):
    """Draw serial port UI elements (text input and received messages)"""
    img_h, img_w = img.shape[:2]
    
    # Draw text input box
    input_box = get_serial_input_box()
    
    # Input box background
    border_color = (0, 255, 0) if serial_input_active else (80, 80, 80)
    cv2.rectangle(img, (input_box['x'], input_box['y']), 
                  (input_box['x'] + input_box['w'], input_box['y'] + input_box['h']), 
                  (40, 40, 40), -1)
    cv2.rectangle(img, (input_box['x'], input_box['y']), 
                  (input_box['x'] + input_box['w'], input_box['y'] + input_box['h']), 
                  border_color, 1)
    
    # Draw input text with cursor
    display_text = serial_tx_buffer
    if serial_input_active:
        display_text += "|"  # Cursor
    cv2.putText(img, display_text, (input_box['x'] + 5, input_box['y'] + 17), 
                cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)
    
    # Draw received messages in bottom-left corner
    if serial_rx_buffer:
        rx_y = img_h - 20
        cv2.putText(img, "Serial RX:", (10, rx_y - len(serial_rx_buffer) * 15 - 5), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.35, (100, 200, 100), 1)
        for i, msg in enumerate(serial_rx_buffer[-5:]):  # Show last 5 messages
            # Truncate long messages
            display_msg = msg[:60] + "..." if len(msg) > 60 else msg
            cv2.putText(img, display_msg, (10, rx_y - (len(serial_rx_buffer[-5:]) - 1 - i) * 15), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.35, (150, 200, 150), 1)
    
    return img

# ---- Histogram settings ----
HIST_WIDTH = 640
HIST_HEIGHT = 128
HIST_BINS = 512  # Number of bins for 16-bit data (downsampled for display)

def draw_histogram(img, gray16):
    """Draw a histogram of the 16-bit image data in the bottom-right corner"""
    img_h, img_w = img.shape[:2]
    
    # Position histogram in bottom-right with margin
    margin = 10
    hist_x = img_w - HIST_WIDTH - margin
    hist_y = img_h - HIST_HEIGHT - margin
    
    # Calculate histogram with 256 bins for 16-bit data (0-65535)
    hist, _ = np.histogram(gray16.flatten(), bins=HIST_BINS, range=(0, 65536))
    
    # Normalize histogram to fit in display height
    if hist.max() > 0:
        hist_normalized = (hist / hist.max() * (HIST_HEIGHT - 20)).astype(np.int32)
    else:
        hist_normalized = np.zeros(HIST_BINS, dtype=np.int32)
    
    # Draw semi-transparent background
    overlay = img.copy()
    cv2.rectangle(overlay, (hist_x - 5, hist_y - 20), (hist_x + HIST_WIDTH + 5, hist_y + HIST_HEIGHT + 5), (30, 30, 30), -1)
    cv2.addWeighted(overlay, 0.7, img, 0.3, 0, img)
    
    # Draw border
    cv2.rectangle(img, (hist_x - 5, hist_y - 20), (hist_x + HIST_WIDTH + 5, hist_y + HIST_HEIGHT + 5), (80, 80, 80), 1)
    
    # Draw histogram title
    cv2.putText(img, "Histogram (16-bit)", (hist_x, hist_y - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (200, 200, 200), 1)
    
    # Draw histogram bars
    for i in range(HIST_BINS):
        bar_height = hist_normalized[i]
        if bar_height > 0:
            x = hist_x + i
            y_bottom = hist_y + HIST_HEIGHT
            y_top = y_bottom - bar_height
            cv2.line(img, (x, y_bottom), (x, y_top), (100, 200, 100), 1)
    
    # Draw axis labels
    cv2.putText(img, "0", (hist_x, hist_y + HIST_HEIGHT + 12), cv2.FONT_HERSHEY_SIMPLEX, 0.25, (150, 150, 150), 1)
    cv2.putText(img, "65535", (hist_x + HIST_WIDTH - 25, hist_y + HIST_HEIGHT + 12), cv2.FONT_HERSHEY_SIMPLEX, 0.25, (150, 150, 150), 1)
    
    # Draw min/max/mean info
    min_val = int(gray16.min())
    max_val = int(gray16.max())
    mean_val = int(gray16.mean())
    info_text = f"Min:{min_val} Max:{max_val} Mean:{mean_val}"
    cv2.putText(img, info_text, (hist_x, hist_y + HIST_HEIGHT + 22), cv2.FONT_HERSHEY_SIMPLEX, 0.28, (180, 180, 180), 1)
    
    # Draw markers for current offset/range if manual mode
    if not auto_range:
        # Draw offset marker (red line)
        offset_x = hist_x + int(manual_offset / 65535 * HIST_WIDTH)
        cv2.line(img, (offset_x, hist_y), (offset_x, hist_y + HIST_HEIGHT), (0, 0, 255), 1)
        
        # Draw range end marker (blue line)
        range_end = min(manual_offset + manual_range, 65535)
        range_x = hist_x + int(range_end / 65535 * HIST_WIDTH)
        cv2.line(img, (range_x, hist_y), (range_x, hist_y + HIST_HEIGHT), (255, 100, 100), 1)
    
    return img

def draw_pixel_values(img, gray16, mx, my, radius=10, low_byte=None, high_byte=None):
    """Draw pixel values in a radius around the mouse cursor
    
    Shows 16-bit pixel values from gray16 as an overlay near the cursor.
    Only displays when mouse is within the image bounds.
    Also shows the raw high and low bytes for debugging.
    """
    img_h, img_w = img.shape[:2]
    g16_h, g16_w = gray16.shape[:2]
    
    # Check if mouse is within image bounds (use gray16 dimensions)
    if mx < 0 or my < 0 or mx >= g16_w or my >= g16_h:
        return img
    
    # Calculate the area to sample (10 pixel radius = 21x21 grid)
    x_start = max(0, mx - radius)
    x_end = min(g16_w, mx + radius + 1)
    y_start = max(0, my - radius)
    y_end = min(g16_h, my + radius + 1)
    
    # Get statistics for the region from the 16-bit data
    region = gray16[y_start:y_end, x_start:x_end].astype(np.float64)
    if region.size == 0:
        return img
    
    # Use the actual 16-bit values
    center_val = int(gray16[my, mx])
    region_min = int(region.min())
    region_max = int(region.max())
    region_mean = float(region.mean())
    region_std = float(region.std())
    
    # Get raw high/low byte values for debugging
    high_val = int(high_byte[my, mx]) if high_byte is not None else 0
    low_val = int(low_byte[my, mx]) if low_byte is not None else 0
    
    # Prepare info text lines
    info_lines = [
        f"Pos: ({mx}, {my})",
        f"16bit: {center_val}",
        f"Raw High: {high_val} Raw Low: {low_val}",
        f"Region {2*radius+1}x{2*radius+1}:",
        f"  Min: {region_min}",
        f"  Max: {region_max}",
        f"  Mean: {region_mean:.1f}",
        f"  Std: {region_std:.1f}",
    ]
    
    # Calculate box dimensions
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.35
    thickness = 1
    line_height = 14
    padding = 5
    
    max_text_width = 0
    for line in info_lines:
        text_size = cv2.getTextSize(line, font, font_scale, thickness)[0]
        max_text_width = max(max_text_width, text_size[0])
    
    box_width = max_text_width + 2 * padding
    box_height = len(info_lines) * line_height + 2 * padding
    
    # Position the box near the cursor (offset to avoid covering the area)
    box_x = mx + 20
    box_y = my - box_height // 2
    
    # Ensure box stays within image bounds
    if box_x + box_width > img_w:
        box_x = mx - box_width - 20
    if box_y < 0:
        box_y = 0
    if box_y + box_height > img_h:
        box_y = img_h - box_height
    
    # Draw semi-transparent background
    overlay = img.copy()
    cv2.rectangle(overlay, (box_x, box_y), (box_x + box_width, box_y + box_height), (30, 30, 30), -1)
    cv2.addWeighted(overlay, 0.8, img, 0.2, 0, img)
    
    # Draw border
    cv2.rectangle(img, (box_x, box_y), (box_x + box_width, box_y + box_height), (100, 100, 100), 1)
    
    # Draw text lines
    for i, line in enumerate(info_lines):
        text_y = box_y + padding + (i + 1) * line_height - 3
        cv2.putText(img, line, (box_x + padding, text_y), font, font_scale, (255, 255, 255), thickness)
    
    # Draw cursor crosshair
    cv2.line(img, (mx - 5, my), (mx + 5, my), (0, 255, 255), 1)
    cv2.line(img, (mx, my - 5), (mx, my + 5), (0, 255, 255), 1)
    
    # Draw circle showing the sampled region
    cv2.circle(img, (mx, my), radius, (0, 255, 255), 1)
    
    return img

def apply_sharpening(img):
    """Apply unsharp mask sharpening to the image based on current setting
    
    Unsharp masking: sharpened = original + strength * (original - blurred)
    This method enhances edges without amplifying noise like kernel sharpening.
    """
    level = sharpen_levels[sharpen_index]
    params = sharpen_params[level]
    
    if params is None:
        return img
    
    blur_size, strength = params
    
    # Apply Gaussian blur
    blurred = cv2.GaussianBlur(img, (blur_size, blur_size), 0)
    
    # Unsharp mask: original + strength * (original - blurred)
    # Using float to avoid overflow
    sharpened = img.astype(np.float32) + strength * (img.astype(np.float32) - blurred.astype(np.float32))
    
    # Clip to valid range
    sharpened = np.clip(sharpened, 0, 255).astype(np.uint8)
    return sharpened

def apply_palette(gray8):
    """Apply current palette to grayscale image (with optional inversion)"""
    # Apply inversion if enabled
    img = 255 - gray8 if invert_palette else gray8
    
    palette_name = palette_names[current_palette_index]
    if palette_name == 'grayscale':
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    else:
        return cv2.applyColorMap(img, getattr(cv2, f'COLORMAP_{palette_name.upper()}'))

# ---- open ----
cap = cv2.VideoCapture(cam_index, cv2.CAP_AVFOUNDATION)
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 2560)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1024)

# Create window and set mouse callback
WINDOW_NAME = "Titan1280"
cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_AUTOSIZE)
cv2.setMouseCallback(WINDOW_NAME, mouse_callback)

# ---- loop ----
while True:
    ret, frame = cap.read()
    if not ret:
        break
    
    # Check if window was closed (X button)
    if cv2.getWindowProperty(WINDOW_NAME, cv2.WND_PROP_VISIBLE) < 1:
        break

    h, w, c = frame.shape  # this is BGR (driver converted)

    # split halves
    b1  = frame[:, :1280, :]
    b2 = frame[:, 1280:, :]

    # first half uses BLUE, second uses GREEN
    low  = b2[:, :, 1].astype(np.uint16)
    high = b1[:, :, 1].astype(np.uint16)

    gray16 = (high << 8) | low
    current_gray16 = gray16.copy()

    # Apply normalization (auto or manual)
    if auto_range:
        # Use percentile-based normalization to clip outliers
        low_val = np.percentile(gray16, AUTO_RANGE_LOW_PERCENTILE)
        high_val = np.percentile(gray16, AUTO_RANGE_HIGH_PERCENTILE)
        
        # Ensure we have a valid range
        if high_val <= low_val:
            high_val = low_val + 1
        
        # Normalize using the percentile range
        gray_float = gray16.astype(np.float32)
        gray_float = (gray_float - low_val) / (high_val - low_val) * 255.0
        gray_float = np.clip(gray_float, 0, 255)
        preview8 = gray_float.astype(np.uint8)
    else:
        # Manual offset and range
        # Clip values to the offset/range window and scale to 0-255
        gray_float = gray16.astype(np.float32)
        gray_float = (gray_float - manual_offset) / manual_range * 255.0
        gray_float = np.clip(gray_float, 0, 255)
        preview8 = gray_float.astype(np.uint8)
    
    # Apply sharpening (before color mapping for better results)
    preview8 = apply_sharpening(preview8)
    
    # Apply false color palette
    preview = apply_palette(preview8)
    
    # Store clean preview without UI elements (for PNG saving)
    current_preview = preview.copy()
    
    # Draw UI elements (only for display, not saved)
    preview = draw_buttons(preview)
    preview = draw_sliders(preview)
    preview = draw_serial_ui(preview)
    if show_histogram:
        preview = draw_histogram(preview, gray16)
    
    # Draw pixel values around mouse cursor (if enabled)
    if show_cursor_readout:
        preview = draw_pixel_values(preview, gray16, mouse_x, mouse_y, radius=10, low_byte=low, high_byte=high)
    
    # Write to video if recording
    if recording and video_writer is not None and video_writer.isOpened():
        #video_writer.write(preview)   # write the displayed RGB frame (with UI)
        video_writer.write(current_preview)  # clean frame without buttons

    cv2.imshow(WINDOW_NAME, preview)

    key = cv2.waitKey(1) & 0xFF
    if key == 27:  # ESC key
        serial_input_active = False  # Deactivate input on ESC
        break
    
    # Handle keyboard input for serial text box
    if serial_input_active:
        if key == 13:  # Enter key - send message
            if serial_tx_buffer:
                serial_send(serial_tx_buffer)
                serial_tx_buffer = ""
        elif key == 8:  # Backspace
            serial_tx_buffer = serial_tx_buffer[:-1]
        elif key == 27:  # ESC - deactivate input
            serial_input_active = False
        elif 32 <= key <= 126:  # Printable ASCII characters
            serial_tx_buffer += chr(key)

# Save settings before exit
save_settings()

# Disconnect serial if connected
disconnect_serial()

# Make sure video is properly closed if still recording
if recording and video_writer:
    video_writer.release()
    print("Video writer closed on program exit")

cap.release()
cv2.destroyAllWindows()
