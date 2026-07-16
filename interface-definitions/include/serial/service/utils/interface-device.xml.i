<!-- include start from serial/service/utils/interface-device.xml.i -->
<leafNode name="device">
  <properties>
    <help>Specifies the serial port to be used (required)</help>
    <completionHelp>
      <script>${vyos_completion_dir}/list_serial.py --selector all</script>
    </completionHelp>
    <valueHelp>
      <format>ttySxxx</format>
      <description>Regular serial interface</description>
    </valueHelp>
    <constraint>
      <validator name="tty-port"/>
    </constraint>
  </properties>
</leafNode>
<!-- include end -->
