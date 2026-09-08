<!-- include start from serial/general/flow-control-none-and-software.xml.i -->
<leafNode name="flow-control">
  <properties>
    <help>Flow control</help>
    <completionHelp>
      <list>none software</list>
    </completionHelp>
    <valueHelp>
      <format>none</format>
      <description>No flow control</description>
    </valueHelp>
    <valueHelp>
      <format>software</format>
      <description>Software flow control (XON/XOFF)</description>
    </valueHelp>
    <constraint>
      <regex>(none|software)</regex>
    </constraint>
  </properties>
  <defaultValue>none</defaultValue>
</leafNode>
<!-- include end -->
