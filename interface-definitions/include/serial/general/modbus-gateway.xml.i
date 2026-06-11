<!-- include start from serial/general/modbus-gateway.xml.i -->
<node name="modbus-gateway">
  <properties>
    <help>Modbus gateway setting</help>
  </properties>
  <children>
    <leafNode name="allow-broadcast">
      <properties>
        <help>Enable serial Modbus broadcasts</help>
        <valueless/>
      </properties>
    </leafNode>
    <leafNode name="character-timeout">
      <properties>
        <help>Specifies how long to wait, after a character to determine the end of frame (in ms)</help>
        <valueHelp>
          <format>u32:10-10000</format>
          <description>Decimal integer (10-10000)</description>
        </valueHelp>
        <constraint>
          <validator name="numeric" argument="--range 10-10000"/>
        </constraint>
      </properties>
      <defaultValue>30</defaultValue>
    </leafNode>
    <leafNode name="disable-exceptions">
      <properties>
        <help>Disable Modbus exceptions</help>
        <valueless/>
      </properties>
    </leafNode>
    <leafNode name="idle-timeout">
      <properties>
        <help>Use this timer to close a connection because of inactivity (in s)</help>
        <valueHelp>
          <format>u32:0-300</format>
          <description>Decimal integer (0-300)</description>
        </valueHelp>
        <constraint>
          <validator name="numeric" argument="--range 0-300"/>
        </constraint>
      </properties>
      <defaultValue>10</defaultValue>
    </leafNode>
    <leafNode name="message-timeout">
      <properties>
        <help>Specifies how long to wait for a response message from a Modbus TCP or serial slave before sending a Modbus exception (in ms)</help>
        <valueHelp>
          <format>u32:10-10000</format>
          <description>Decimal integer (10-10000)</description>
        </valueHelp>
        <constraint>
          <validator name="numeric" argument="--range 10-10000"/>
        </constraint>
      </properties>
      <defaultValue>1000</defaultValue>
    </leafNode>
    <leafNode name="next-request-delay">
      <properties>
        <help>Specifies a delay to allow serial slave to re-enable receivers before issuing next Modbus Master request (in ms)</help>
        <valueHelp>
          <format>u32:0-1000</format>
          <description>Decimal integer (0-1000)</description>
        </valueHelp>
        <constraint>
          <validator name="numeric" argument="--range 0-1000"/>
        </constraint>
      </properties>
      <defaultValue>50</defaultValue>
    </leafNode>
    <leafNode name="port">
      <properties>
        <help>Network port number that the slave gateway will listen on for both TCP and UDP messages</help>
        <valueHelp>
          <format>u32:1-65535</format>
          <description>Port number</description>
        </valueHelp>
        <constraint>
          <validator name="numeric" argument="--range 1-65535"/>
        </constraint>
      </properties>
      <defaultValue>502</defaultValue>
    </leafNode>
    <leafNode name="disable-queuing">
      <properties>
        <help>Disable request-queuing to not allows multiple, simultaneous messages to be queued and processed in order of reception</help>
        <valueless/>
      </properties>
    </leafNode>
    <node name="tls">
      <properties>
        <help>Enable TLS for TCP</help>
      </properties>
      <children>
        <leafNode name="template">
          <properties>
            <help>TLS template name</help>
            <valueHelp>
              <format>txt</format>
              <description>Name of TLS template defined in global-parameters</description>
            </valueHelp>
            <completionHelp>
              <path>serial global tls template</path>
            </completionHelp>
          </properties>
        </leafNode>
      </children>
    </node>
  </children>
</node>
<!-- include end -->
