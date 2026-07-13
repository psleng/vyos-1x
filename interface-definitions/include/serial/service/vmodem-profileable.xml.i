<!-- include start from serial/service/vmodem-profileable.xml.i -->
<node name="vmodem">
  <properties>
    <help>Virtual Modem service settings</help>
  </properties>
  <children>
    #include <include/serial/service/utils/keepalive.xml.i>
    #include <include/serial/service/utils/tls-port.xml.i>
    #include <include/serial/service/utils/transmit-string-no-end.xml.i>
    <leafNode name="echo">
      <properties>
        <help>Enable echo characters in command mode</help>
        <valueless/>
      </properties>
    </leafNode>
    <leafNode name="initialization-string">
      <properties>
        <help>String that is sent to the modem when a connection succeeds</help>
        <constraint>
          <regex>.{0,254}</regex>
        </constraint>
        <constraintErrorMessage>Vmodem modem initialization string too long (limit 254 characters)</constraintErrorMessage>
      </properties>
    </leafNode>
    <leafNode name="response-delay">
      <properties>
        <help>The amount of time, in milliseconds, before an AT response is sent to the requesting device</help>
        <valueHelp>
          <format>u32:0-999</format>
          <description>Specifies the delay in milliseconds</description>
        </valueHelp>
        <constraint>
          <validator name="numeric" argument="--range 0-999"/>
        </constraint>
      </properties>
      <defaultValue>250</defaultValue>
    </leafNode>
    <node name="send-connect-status">
      <properties>
        <help>Send connection status as</help>
      </properties>
      <children>
        <leafNode name="numeric">
          <properties>
            <help>Send connection status as numeric (default)</help>
            <valueless/>
          </properties>
        </leafNode>
        <node name="verbose">
          <properties>
            <help>Send connection status as verbose</help>
          </properties>
          <children>
            <leafNode name="failure-string">
              <properties>
                <help>String that is sent to the serial device when a connection fails</help>
                <constraint>
                  <regex>.{0,30}</regex>
                </constraint>
                <constraintErrorMessage>Vmodem failure string too long (limit 30 characters)</constraintErrorMessage>
              </properties>
              <defaultValue>NO&#160;CARRIER</defaultValue>
            </leafNode>
            <leafNode name="success-string">
              <properties>
                <help>String that is sent to the serial device when a connection succeeds</help>
                <constraint>
                  <regex>.{0,40}</regex>
                </constraint>
                <constraintErrorMessage>Vmodem success string too long (limit 40 characters)</constraintErrorMessage>
              </properties>
              <defaultValue>CONNECT</defaultValue>
            </leafNode>
          </children>
        </node>
      </children>
    </node>
    <node name="hardware-signals">
      <properties>
        <help>Hardware signals assignment</help>
      </properties>
      <children>
        <leafNode name="dtr">
          <properties>
            <help>DTR signal assignment</help>
            <completionHelp>
              <list>always-on acts-as-dcd acts-as-ri</list>
            </completionHelp>
            <constraint>
              <regex>(always-on|acts-as-dcd|acts-as-ri)</regex>
            </constraint>
          </properties>
          <defaultValue>always-on</defaultValue>
        </leafNode>
        <leafNode name="rts">
          <properties>
            <help>RTS signal assignment</help>
            <completionHelp>
              <list>always-on acts-as-dcd acts-as-ri</list>
            </completionHelp>
            <constraint>
              <regex>(always-on|acts-as-dcd|acts-as-ri)</regex>
            </constraint>
          </properties>
          <defaultValue>always-on</defaultValue>
        </leafNode>
        <leafNode name="dcd">
          <properties>
            <help>DCD signal assignment</help>
            <completionHelp>
              <list>always-on on-when-host-connect</list>
            </completionHelp>
            <constraint>
              <regex>(always-on|on-when-host-connect)</regex>
            </constraint>
          </properties>
          <defaultValue>always-on</defaultValue>
        </leafNode>
      </children>
    </node>
    <node name="client">
      <properties>
          <help>Virtual Modem client settings</help>
      </properties>
      <children>
        #include <include/serial/service/utils/remote.xml.i>
      </children>
    </node>
  </children>
</node>
<!-- include end -->
