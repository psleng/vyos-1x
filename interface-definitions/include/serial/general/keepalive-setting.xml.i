<!-- include start from serial/general/keepalive-setting.xml.i -->
<node name="keep-alive">
  <properties>
    <help>Keep-alive global setting</help>
  </properties>
  <children>
    <leafNode name="interval">
      <properties>
        <help>Specify how often, in seconds, the device will send a TCP keep-alive to services that support TCP keep-alive</help>
        <valueHelp>
          <format>u32:1-32767</format>
          <description>Decimal integer (1-32767)</description>
        </valueHelp>
        <constraint>
          <validator name="numeric" argument="--range 1-32767"/>
        </constraint>
      </properties>
      <defaultValue>180</defaultValue>
    </leafNode>
    <leafNode name="retry">
      <properties>
        <help>The number of TCP keepalive retries before the connection is closed</help>
        <valueHelp>
          <format>u32:1-32767</format>
          <description>Decimal integer (1-32767)</description>
        </valueHelp>
        <constraint>
          <validator name="numeric" argument="--range 1-32767"/>
        </constraint>
      </properties>
      <defaultValue>5</defaultValue>
    </leafNode>
    <leafNode name="retry-timeout">
      <properties>
        <help>Sets the maximum time, in seconds, to wait for a response after sending a TCP keep-alive message</help>
        <valueHelp>
          <format>u32:1-32767</format>
          <description>Decimal integer (1-32767)</description>
        </valueHelp>
        <constraint>
          <validator name="numeric" argument="--range 1-32767"/>
        </constraint>
      </properties>
      <defaultValue>5</defaultValue>
    </leafNode>
  </children>
</node>
<!-- include end -->
