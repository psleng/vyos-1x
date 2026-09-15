<!-- include start from serial/service/utils/transmit-string-end.xml.i -->
<node name="transmit-string">
  <properties>
    <help>Transmit string settings</help>
  </properties>
  <children>
    <leafNode name="at-end">
      <properties>
        <help>String to transmit when session terminates</help>
        <constraint>
          <regex>.{0,127}</regex>
        </constraint>
        <constraintErrorMessage>String to transmit when session terminates too long (limit 127 characters)</constraintErrorMessage>
      </properties>
    </leafNode>
  </children>
</node>
<!-- include end -->
