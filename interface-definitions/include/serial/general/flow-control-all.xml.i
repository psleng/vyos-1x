<!-- include start from serial/general/flow-control-all.xml.i -->
<leafNode name="flow-control">
  <properties>
    <help>Flow control</help>
    <completionHelp>
      <list>both none hardware software</list>
    </completionHelp>
    <valueHelp>
      <format>both</format>
      <description>Both hardware and software flow control</description>
    </valueHelp>
    <valueHelp>
      <format>none</format>
      <description>No flow control</description>
    </valueHelp>
    <valueHelp>
      <format>hardware</format>
      <description>Hardware flow control (RTS/CTS)</description>
    </valueHelp>
    <valueHelp>
      <format>software</format>
      <description>Software flow control (XON/XOFF)</description>
    </valueHelp>
    <constraint>
      <regex>(both|none|hardware|software)</regex>
    </constraint>
  </properties>
  <defaultValue>none</defaultValue>
</leafNode>
<!-- include end -->
