<!-- include start from serial/service/utils/modem.xml.i -->
<node name="modem">
  <properties>
    <help>Modem setting</help>
  </properties>
  <children>
    <leafNode name="connection">
      <properties>
        <help>Modem connection direction</help>
        <completionHelp>
          <list>in out both</list>
        </completionHelp>
        <constraint>
          <regex>(in|out|both)</regex>
        </constraint>
      </properties>
    </leafNode>
    <leafNode name="initialization-string">
      <properties>
        <help>A series of commands sent to the modem by a communications program at start up</help>
        <constraint>
          <regex>.{0,61}</regex>
        </constraint>
        <constraintErrorMessage>Initialization string too long (limit 61 characters)</constraintErrorMessage>
      </properties>
    </leafNode>
    <node name="dial">
      <properties>
        <help>Dial setting</help>
      </properties>
      <children>
        <leafNode name="phone-number">
          <properties>
            <help>The phone number to use when dial out is enabled</help>
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
      </children>
    </node>
  </children>
</node>
<!-- include end -->
