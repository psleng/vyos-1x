<!-- include start from serial/service/utils/listen-port.xml.i -->
<leafNode name="port">
  <properties>
    <help>TCP port to listen for incoming connections</help>
    <valueHelp>
      <format>u32:1-65535</format>
      <description>Port number</description>
    </valueHelp>
    <constraint>
      <validator name="numeric" argument="--range 1-65535"/>
    </constraint>
  </properties>
</leafNode>
<!-- include end -->
