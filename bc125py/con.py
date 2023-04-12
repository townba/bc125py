import os
import glob
import sys
import time
from bc125py.app import log
try:
	import serial
except ImportError:
	class serial: Serial = None
	# We also try PyUSB
	pass
try:
	import usb
	from usb.backend import libusb1
except ImportError:
	class usb:
		class core:
			Device = None
			Endpoint = None
	if not "serial" in sys.modules:
		log.error("Neither pySerial nor PyUSB were loaded (imports failed)")


class CommandError(RuntimeError):
	"""Error resulting from an invalid scanner command

	Args:
		RuntimeError (str): Error message
	"""

	def __init__(self, message: str = "A command error has occurred"):
		super().__init__(message)


class ScannerConnection:
	"""A connection to the scanner
	"""

	connected: bool = False
	__serial: serial.Serial = None
	__dev: usb.core.Device = None
	__ep_in: usb.core.Endpoint = None
	__ep_out: usb.core.Endpoint = None


	def __init__(self):
		pass


	def connect(self, port: str = None) -> None:
		"""Establish a connection to the scanner

		Args:
			device_path (str, optional): Force connection to specific device. Defaults to None.

		Raises:
			ConnectionError: If the connection is already established, or if any errors occur while connecting
		"""

		if self.connected:
			raise ConnectionError("Connection already established")

		if "serial" in sys.modules:
			try:
				# First, set up device driver. It doesn't matter if we do this multiple times
				ScannerConnection.__setup_driver()

				# Second, determine device path
				if not port:
					found_ports = ScannerConnection.find_ports()

					if len(found_ports) < 1:
						raise ConnectionError("Could not find any scanner")

					port = found_ports[0]

				log.debug("con: using port: " + port)
			except ConnectionError as e:
				# We'll try PyUSB next.
				pass

		# Third, establish a device connection.
		self.__open_connection(port)

		self.connected = True
		log.debug("con: connection successfully established")


	def __open_connection(self, port: str) -> None:
		"""internal use. Open serial connection to device

		Args:
			port (str): open this device

		Raises:
			ConnectionError: if connection fails
		"""

		if "serial" in sys.modules and port != usb:
			# Now, try to open the device file
			try:
				self.__serial = serial.Serial(port)
				self.__serial.timeout = 120
				self.__serial.reset_input_buffer()
				self.__serial.reset_output_buffer()
				return

			except serial.SerialException as e:
				raise ConnectionError("Error connecting to scanner: " + str(e))

		if not "usb" in sys.modules:
			raise ConnectionError("Error connecting to scanner: No communication method available.")

		# Try to open the device file
		try:
			self.__dev = usb.core.find(
				idVendor=0x1965, idProduct=0x0017,
				custom_match=lambda dev:
					usb.util.get_string(dev, dev.iProduct) in ("BC125AT", "UBC125XLT", "UBC126AT"))
			if self.__dev is None:
			  raise FileNotFoundError("BC125AT or variant not found.")

			self.__dev.set_configuration()
			cfg = self.__dev.get_active_configuration()
			intf = next((intf for intf in cfg if intf.bInterfaceClass == 0xA), None)
			if intf is None:
				raise FileNotFoundError("Interface not found.")

			self.__ep_in = usb.util.find_descriptor(
				intf,
				custom_match = lambda e:
					usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
			self.__ep_out = usb.util.find_descriptor(
				intf,
				custom_match = lambda e:
					usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
			if self.__ep_in is None or self.__ep_out is None:
				raise FileNotFoundError("Endpoint(s) not found.")

		except Exception as e:
			raise ConnectionError("Error connecting to scanner: " + str(e))


	def _exec(self, command: str) -> str:
		"""INTERNAL USE! USE exec() INSTEAD! -- Execute a command

		Args:
			command (str): the command to execute

		Raises:
			ConnectionError: if the command fails to send
			ConnectionError: if the response cannot be read

		Returns:
			str: device response
		"""

		# First, try to send command to device
		try:
			# Don't forget to append a \r. It's the 125AT's line ending
			send_data: bytes = bytes(command + "\r", "ascii")
			# Send command
			log.debug("con_exec: send:", send_data)
			if self.__serial:
				self.__serial.write(send_data)
			elif self.__ep_out:
				self.__ep_out.write(send_data, timeout=120000)
		except Exception as e:
			raise ConnectionError("Could not communicate (write) with scanner: " + str(e))

		# Response variable
		resp: bytes = b""

		# Read data from scanner
		try:
			if self.__serial:
				resp = self.__serial.read_until(b"\r")
			elif self.__ep_in:
				while True:
					resp += self.__ep_in.read(16384, timeout=120000).tobytes()
					if resp[-1] == b"\r"[0]:
						break
				resp = resp[:-1]

			log.debug("con_exec: resp:", resp)
		except Exception as e:
			raise ConnectionError("Could not communicate (read) with scanner: " + str(e))

		# Decode and return response
		return resp.decode("ascii").rstrip()


	def exec(self, command, echo: bool = False, return_tuple: bool = True, allow_error = False):
		"""Execute a command on the scanner. Get response.

		Args:
			command (tuple, str): The command to execute, in string or tuple form
			echo (bool, optional): Should the response include the command name? Defaults to False.
			return_tuple (bool, optional): Should the response be in tuple form? Defaults to True.
			allow_error (bool, optional): Should we allow an invalid command? Defaults to False.

		Raises:
			ConnectionError: if a connection was never established
			ConnectionError: if there is an error communicating with the scanner
			bc125py.CommandError: if the command produces an error

		Returns:
			tuple, str: The command response in tuple or string form
		"""

		if not self.connected:
			raise ConnectionError("Cannot execute command when scanner isn't connected")

		# Convert tuple command to command string
		if type(command) is tuple:
			command = ",".join(map(str, command))
		elif type(command) is not str:
			raise TypeError("exec() command must be str or tuple")

		# Execute command, store result
		resp = self._exec(command)

		# Make sure command executed properly
		if not allow_error:
			if resp.endswith( ("ERR", "NG") ):
				raise CommandError("Error in command: " + command)

		# If echo is off (default), remove the command name from the response
		if not echo:
			resp = resp[4:]

		# If we want the result as a tuple (default), create tuple
		if return_tuple:
			resp = tuple(resp.split(","))


		return resp


	def close(self) -> None:
		"""Disconnect scanner. Safely closes connection.

		Raises:
			ConnectionError: if the scanner never was connected.
		"""

		if not self.connected:
			raise ConnectionError("Can't close closed connection")
		if self.__serial:
			self.__serial.close()
		self.connected = False
		log.debug("con: connection closed")


	def disconnect(self) -> None:
		"""Alias to close()
		"""

		self.close()


	def __del__(self):
		if self.connected:
			self.close()
	

	@staticmethod
	def find_ports(legacy_detection = False) -> list:
		"""Find likely scanner device file

		Returns:
			list: list of potential device files
		"""

		# First, set up device driver. It doesn't matter if we do this multiple times
		try:
			ScannerConnection.__setup_driver()
		except ConnectionError as e:
			# We'll try PyUSB.
			return [usb]

		# Create array for all possible found results
		found_ports = []

		# Try to find scanner ports with pySerial
		try:
			# Import port finder function
			from serial.tools.list_ports import comports

			# Loop through comports. Add those with the 125AT's product id
			for port in comports():
				if port.pid == 23: # BC125AT product id 0017 (hex) -> 23
					found_ports.append(port.device)
		except Exception as e:
			log.debug("con: pyserial failed finding ports. falling back to legacy detection... " + str(e))
			legacy_detection = True


		# These are legacy patterns. Still useful if pySerial doesn't find any ports for some reason
		if legacy_detection:
			found_ports.extend(glob.glob("/dev/serial/by-id/*BC125AT*"))
			found_ports.extend(glob.glob("/dev/serial/by-id/*BC126AT*")) # international version
			found_ports.extend(glob.glob("/dev/ttyACM*"))

		log.debug(
			"find_ports,",
			"legacy-mode:", legacy_detection, "-",
			found_ports
		)
		return found_ports
	

	@staticmethod
	def __setup_driver() -> None:
		"""internal use. inject driver string into kernel

		Raises:
			ConnectionError: if no scanner detected
			ConnectionError: if error writing to new_id file
		"""

		try:
			# Path to new acm device file
			driver_path: str = "/sys/bus/usb/drivers/cdc_acm/new_id"

			# Make directories up to this file
			# They likely do not exist
			os.makedirs(os.path.dirname(driver_path), exist_ok=True)

		except IOError as e:
			raise ConnectionError("No scanner found")
		
		try:
			# Open new_id file, write driver string
			driver_file = open(driver_path, "w")
			print("1965 0017 2 076d 0006", file=driver_file) # Thanks to Rikus Goodell's bc125at-perl
			driver_file.close()
			
			log.debug("con: successfully setup driver string")

		except IOError as e:
			raise ConnectionError("Error setting up driver: " + str(e))
		
		# Pause to give the OS time to generate the device file
		time.sleep(0.1)


class SimulatedScannerConnection(ScannerConnection):
	"""A simulated scanner connection.
	All commands are logged to the specified "port".
	For debugging purposes.
	"""

	connected: bool = False
	__log_file = None # file


	def __init__(self, log_file_path: str = None):
		if log_file_path:
			self.connect(log_file_path)


	def connect(self, port: str = "/dev/null") -> None:
		"""Establish a connection to the scanner

		Args:
			port (str): The port (log file) to write to.

		Raises:
			ConnectionError: If the connection is already established, or if any errors occur while connecting
		"""

		if self.connected:
			raise ConnectionError("Connection already established")

		# Try to open file in write mode
		try:
			self.__log_file = open(port, "w")
		except IOError as e:
			raise ConnectionError("Could not connect to simulated port -", str(e))

		self.connected = True
		log.debug("con: SIMULATED connection successfully established at port", port)


	def __setup_driver(self) -> None:
		pass


	def __find_ports(self) -> list:
		return []


	def __open_connection(self, port: str) -> None:
		pass


	def _exec(self, command: str) -> str:
		"""INTERNAL USE! USE exec() INSTEAD! -- Execute a command

		Args:
			command (str): the command to execute

		Raises:
			ConnectionError: if the command fails to send

		Returns:
			str: inputted command
		"""

		# First, try to send command to device
		try:
			self.__log_file.write(command + "\n")
			log.debug("con_exec: send:", command)
		except IOError as e:
			raise ConnectionError("Could not communicate (write) with scanner: " + str(e))

		# Simulated connection; return input
		return command


	def exec(self, command, echo: bool = False, return_tuple: bool = True, allow_error = False):
		"""Execute a command on the scanner. Get response.

		Args:
			command (tuple, str): The command to execute, in string or tuple form
			echo (bool, optional): Should the response include the command name? Defaults to False.
			return_tuple (bool, optional): Should the response be in tuple form? Defaults to True.
			allow_error (bool, optional): Should we allow an invalid command? Defaults to False.

		Raises:
			ConnectionError: if a connection was never established
			ConnectionError: if there is an error communicating with the scanner
			bc125py.CommandError: if the command produces an error

		Returns:
			tuple, str: The command response in tuple or string form
		"""

		if not self.connected:
			raise ConnectionError("Cannot execute command when scanner isn't connected")

		# Convert tuple command to command string
		if type(command) is tuple:
			command = ",".join(map(str, command))
		elif type(command) is not str:
			raise TypeError("exec() command must be str or tuple")

		# Execute command, store result
		resp = self._exec(command)

		# If we want the result as a tuple (default), create tuple
		if return_tuple:
			resp = tuple(resp.split(","))

		return resp


	def close(self) -> None:
		"""Disconnect scanner. Safely closes connection.

		Raises:
			ConnectionError: if the scanner never was connected.
		"""

		if not self.connected:
			raise ConnectionError("Can't close closed connection")
		self.__log_file.close()
		self.connected = False
		log.debug("con: connection closed")


	def disconnect(self) -> None:
		"""Alias to close()
		"""

		self.close()


	def __del__(self):
		if self.connected:
			self.close()
