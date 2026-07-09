<!-- include start from serial/service/utils/modem-dial-outbound-config.xml.i -->
<leafNode name="phone-number">
  <properties>
    <help>The phone number to use to dial out</help>
    <constraint>
      <regex>.{0,31}</regex>
    </constraint>
    <constraintErrorMessage>Phone number too long (limit 31 characters)</constraintErrorMessage>
  </properties>
</leafNode>
<leafNode name="retry">
  <properties>
    <help>The number of times the device will attempt to re-establish a connection with a remote modem</help>
    <valueHelp>
      <format>u32:1-99</format>
      <description>Decimal integer (1-99)</description>
    </valueHelp>
    <constraint>
      <validator name="numeric" argument="--range 1-99"/>
    </constraint>
  </properties>
  <defaultValue>2</defaultValue>
</leafNode>
<leafNode name="timeout">
  <properties>
    <help>The number of seconds the device will wait to establish a connection to a remote modem</help>
    <valueHelp>
      <format>u32:1-99</format>
      <description>Decimal integer (1-99)</description>
    </valueHelp>
    <constraint>
      <validator name="numeric" argument="--range 1-99"/>
    </constraint>
  </properties>
  <defaultValue>45</defaultValue>
</leafNode>
<!-- include end -->
